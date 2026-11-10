"""El servidor de Minecraft como proceso hijo del panel y lo que se sabe de él en cada momento.

El panel lanza /start de la imagen de itzg (que descarga Minecraft, Fabric y los mods y acaba en
mc-server-runner -> java). La consola se lee de su salida; los datos (jugadores, MSPT…) por RCON.
"""

import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from rcon import Rcon, RconError

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MC_LINE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \[([^\]]+)/(\w+)\]: (.*)$")
NAME = r"([A-Za-z0-9_]{1,16})"
JOIN = re.compile(rf"^{NAME} joined the game$")
LEAVE = re.compile(rf"^{NAME} left the game$")
CHAT = re.compile(rf"^(?:\[Not Secure\] )?<{NAME}> (.*)$")
ADVANCEMENT = re.compile(rf"^{NAME} has (made the advancement|completed the challenge|reached the goal) \[(.+)\]$")
LIST_UUIDS = re.compile(rf"{NAME} \(([0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}})\)")
LIST_MAX = re.compile(r"of a max of (\d+) players")
MSPT = re.compile(r"Average time per tick: ([\d.]+)\s*ms")
P95 = re.compile(r"P95: ([\d.]+)\s*ms")
TICK_RATE = re.compile(r"Target tick rate: ([\d.]+)")
POS = re.compile(r"\[(-?[\d.E-]+)d, (-?[\d.E-]+)d, (-?[\d.E-]+)d\]")
DIM = re.compile(r'"([a-z0-9_.-]+:[a-z0-9_/.-]+)"')
DAY = re.compile(r"The time is (\d+)")             # hasta la 1.21
CLOCK = re.compile(r"Clock \S+ is at (\d+) tick")   # desde la 26.1 (relojes por dimensión)
# Mensajes de un jugador conectado que no son su muerte.
NOT_DEATH = ("lost connection", "issued server command", "moved too quickly", "moved wrongly", "logged in with",
             "joined the game", "left the game", "has made the advancement", "has completed the challenge",
             "has reached the goal", "was kicked", "[")

CLK_TCK = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


class MCServer:
    def __init__(self, data_dir: Path, build_env, rcon_password, on_activity=None):
        self.data_dir = data_dir
        self.build_env = build_env          # función -> (env, firma) con la configuración actual
        self.rcon = Rcon("127.0.0.1", 25575, rcon_password)
        self.on_activity = on_activity      # se llama mientras haya jugadores (para las copias automáticas)
        self.proc = None
        self.state = "stopped"              # stopped | starting | online | stopping
        self.started_at = None
        self.online_at = None
        self.applied_sig = None
        self.stop_requested = False
        self.crashes = deque(maxlen=5)
        self.exit_code = None
        self.lock = threading.RLock()
        self.log = deque(maxlen=4000)       # (n, texto)
        self.log_n = 0
        self.events = deque(maxlen=120)     # dict: n, t, kind, name, text
        self.event_n = 0
        self.players = {}                   # nombre -> {uuid, since, dim, pos}
        self.max_players = None
        self.perf = {}                      # mspt, p95, tps, tick_rate
        self.history = deque(maxlen=360)    # (t, mspt, jugadores): una muestra cada 10 s, la última hora
        self.proc_stats = {}                # rss, cpu
        self.world_size = None
        self.day = None
        self._java_pid = None
        self._cpu_prev = None
        threading.Thread(target=self._poll_loop, daemon=True).start()

    # ------------------------------------------------------------ registro y eventos

    def _add_log(self, text):
        self.log_n += 1
        self.log.append((self.log_n, text))

    def panel_log(self, text):
        self._add_log(f"[panel] {text}")

    def _event(self, kind, name="", text=""):
        self.event_n += 1
        self.events.append({"n": self.event_n, "t": time.time(), "kind": kind, "name": name, "text": text})

    def _parse(self, line):
        m = MC_LINE.match(line)
        if not m:
            return
        thread, level, msg = m.group(2), m.group(3), m.group(4)
        if msg.startswith("Done (") and "For help" in msg:
            with self.lock:
                if self.state == "starting":
                    self.state = "online"
                    self.online_at = time.time()
            self._event("online", text="Servidor en marcha")
            return
        if msg.startswith("Stopping the server") or msg == "Stopping server":
            self.state = "stopping"
            return
        if thread != "Server thread" or level != "INFO":
            return
        if m2 := JOIN.match(msg):
            self.players.setdefault(m2.group(1), {"uuid": "", "since": time.time(), "dim": "", "pos": None})
            self._event("join", m2.group(1))
        elif m2 := LEAVE.match(msg):
            self.players.pop(m2.group(1), None)
            self._event("leave", m2.group(1))
        elif m2 := CHAT.match(msg):
            self._event("chat", m2.group(1), m2.group(2))
        elif m2 := ADVANCEMENT.match(msg):
            self._event("advancement", m2.group(1), m2.group(3))
        else:
            first = msg.split(" ", 1)
            if len(first) == 2 and first[0] in self.players and not any(s in first[1] for s in NOT_DEATH):
                self._event("death", first[0], first[1])

    def _pump(self, proc):
        for raw in proc.stdout:
            line = ANSI.sub("", raw.rstrip("\n")).replace("\r", "")
            if not line.strip():
                continue
            self._add_log(line)
            try:
                self._parse(line)
            except Exception as e:  # que un mensaje raro nunca pare la lectura de la consola
                self._add_log(f"[panel] no se pudo interpretar una línea: {e}")
        code = proc.wait()
        with self.lock:
            was_online = self.online_at is not None
            self.exit_code = code
            self.state = "stopped"
            self.started_at = self.online_at = None
            self.players.clear()
            self.perf, self.proc_stats = {}, {}
            self._java_pid = self._cpu_prev = None
            self.rcon.close()
            crashed = not self.stop_requested and code != 0
        self.panel_log(f"el servidor se ha parado (código {code})")
        self._event("stopped", text="Servidor parado" if not crashed else f"El servidor se ha caído (código {code})")
        # Si se cae solo después de haber arrancado bien, se vuelve a arrancar (máximo 3 veces en 15 min).
        if crashed and was_online:
            now = time.time()
            self.crashes.append(now)
            if len([t for t in self.crashes if now - t < 900]) <= 3:
                self.panel_log("se ha caído: lo vuelvo a arrancar en 10 s")
                threading.Timer(10, self._restart_after_crash).start()
            else:
                self.panel_log("se ha caído demasiadas veces seguidas: no lo vuelvo a arrancar")

    def _restart_after_crash(self):
        if self.state == "stopped" and not self.stop_requested:
            try:
                self.start()
            except Exception as e:
                self.panel_log(f"no se pudo volver a arrancar: {e}")

    # ------------------------------------------------------------ arrancar y parar

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        with self.lock:
            if self.running():
                return
            env, sig = self.build_env()
            self.stop_requested = False
            self.exit_code = None
            self.state = "starting"
            self.started_at = time.time()
            self.online_at = None
            self.applied_sig = sig
            self.panel_log(f"arrancando Minecraft {env.get('VERSION')} con Fabric…")
            self.proc = subprocess.Popen(
                ["/start"], cwd=self.data_dir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1, start_new_session=True)
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def stop(self, timeout=120):
        """Para el servidor guardando el mundo; espera a que termine."""
        with self.lock:
            if not self.running():
                return
            self.stop_requested = True
            proc, was = self.proc, self.state
            self.state = "stopping"
        self.panel_log("parando el servidor…")
        try:
            if was == "online":
                # mc-server-runner recibe la señal, manda "stop" a Minecraft y espera a que guarde.
                proc.send_signal(signal.SIGTERM)
            else:
                # Aún está descargando o cargando: se corta todo el grupo de procesos.
                os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.panel_log("no termina: lo fuerzo")
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        for _ in range(50):  # que el hilo de la consola apunte la salida antes de volver
            if self.state == "stopped":
                break
            time.sleep(0.1)

    def restart(self):
        self.stop()
        self.start()

    def announce(self, text, seconds):
        """Avisa a los jugadores conectados y espera, antes de parar o reiniciar."""
        if self.state != "online" or not self.players or seconds <= 0:
            return
        try:
            self.rcon.command(f'tellraw @a {{"text":"[Servidor] {text} en {seconds} segundos","color":"yellow"}}')
        except RconError:
            return
        time.sleep(seconds)

    def command(self, cmd, timeout=15):
        """Manda un comando. Con el servidor en marcha va por RCON y devuelve la respuesta;
        mientras arranca va por la consola y la respuesta sale en el registro (devuelve None)."""
        cmd = cmd.strip().lstrip("/")
        if self.state == "online":
            return self.rcon.command(cmd, timeout=timeout)
        if self.running() and self.proc.stdin:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
            return None
        raise RconError("el servidor está parado")

    # ------------------------------------------------------------ datos en vivo

    def _find_java(self):
        if self._java_pid and Path(f"/proc/{self._java_pid}").exists():
            return self._java_pid
        self._java_pid = None
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                cmd = (d / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if cmd and cmd[0].endswith(b"java") and any(b"fabric-server" in c or c.endswith(b".jar") for c in cmd):
                self._java_pid = int(d.name)
                break
        return self._java_pid

    def _read_proc(self):
        pid = self._find_java()
        if not pid:
            return
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            ticks = int(stat[11]) + int(stat[12])
            rss = next(int(line.split()[1]) * 1024 for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                       if line.startswith("VmRSS:"))
        except (OSError, StopIteration, ValueError, IndexError):
            return
        now = time.monotonic()
        cpu = None
        if self._cpu_prev:
            dt = now - self._cpu_prev[0]
            if dt > 0:
                cpu = max(0.0, (ticks - self._cpu_prev[1]) / CLK_TCK / dt / NCPU * 100)
        self._cpu_prev = (now, ticks)
        self.proc_stats = {"rss": rss, "cpu": cpu if cpu is not None else self.proc_stats.get("cpu")}

    def _poll(self, tick):
        out = self.rcon.command("list uuids")
        found = dict(LIST_UUIDS.findall(out))
        if m := LIST_MAX.search(out):
            self.max_players = int(m.group(1))
        now = time.time()
        for name in list(self.players):
            if name not in found:
                self.players.pop(name, None)
        for name, uuid in found.items():
            p = self.players.setdefault(name, {"uuid": uuid, "since": now, "dim": "", "pos": None})
            p["uuid"] = uuid
            try:
                if m := POS.search(self.rcon.command(f"data get entity {name} Pos")):
                    p["pos"] = [round(float(v)) for v in m.groups()]
                if m := DIM.search(self.rcon.command(f"data get entity {name} Dimension")):
                    p["dim"] = m.group(1)
            except (RconError, ValueError):
                pass
        if found and self.on_activity:
            self.on_activity()
        out = self.rcon.command("tick query")
        perf = {}
        if m := MSPT.search(out):
            perf["mspt"] = float(m.group(1))
        if m := P95.search(out):
            perf["p95"] = float(m.group(1))
        rate = float(m.group(1)) if (m := TICK_RATE.search(out)) else 20.0
        perf["tick_rate"] = rate
        if "mspt" in perf:
            perf["tps"] = round(min(rate, 1000 / perf["mspt"]) if perf["mspt"] > 0 else rate, 1)
        perf["frozen"] = "frozen" in out.lower()
        self.perf = perf
        if tick % 2 == 0 and "mspt" in perf:
            self.history.append((round(now), perf["mspt"], len(found)))
        if tick % 12 == 0:
            if m := CLOCK.search(self.rcon.command("time query time")):
                self.day = int(m.group(1)) // 24000
            elif m := DAY.search(self.rcon.command("time query day")):
                self.day = int(m.group(1))

    def _poll_loop(self):
        tick = 0
        while True:
            time.sleep(5)
            if tick % 12 == 0 or self.world_size is None:  # el tamaño del mundo, cada minuto
                world = self.data_dir / "world"
                self.world_size = _dir_size(world) if world.is_dir() else 0
            if self.state == "online":
                self._read_proc()
                try:
                    self._poll(tick)
                except RconError:
                    pass
            elif self.state in ("starting", "stopping"):
                self._read_proc()
            tick += 1

    def snapshot(self):
        now = time.time()
        return {
            "state": self.state,
            "uptime": round(now - self.online_at) if self.online_at else None,
            "starting_for": round(now - self.started_at) if self.started_at and self.state == "starting" else None,
            "exit_code": self.exit_code,
            "players": [{"name": n, "uuid": p["uuid"], "since": round(p["since"]), "dim": p["dim"], "pos": p["pos"]}
                        for n, p in sorted(self.players.items(), key=lambda kv: kv[1]["since"])],
            "max_players": self.max_players,
            "perf": self.perf,
            "proc": self.proc_stats if self.state != "stopped" else {},
            "world_size": self.world_size,
            "day": self.day,
            "history": list(self.history),
        }
