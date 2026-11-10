"""Panel web de un servidor técnico de Minecraft (Fabric + mods de optimización + mapa BlueMap).

El panel lanza el servidor como proceso hijo (/start de la imagen de itzg) con las variables de
entorno que salen de lo elegido en la web, y lo administra por RCON.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file,
                   session, url_for)
from waitress import serve

import backups
import modrinth
from rcon import RconError
from server import MCServer

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PANEL_DIR = DATA_DIR / "panel"
CONFIG_FILE = PANEL_DIR / "config.json"
STATE_FILE = PANEL_DIR / "state.json"
SKINS_DIR = PANEL_DIR / "skins"
MODS_DIR = DATA_DIR / "mods"
BLUEMAP_CONF = DATA_DIR / "config" / "bluemap"
BLUEMAP_WEB_MAPS = DATA_DIR / "bluemap" / "web" / "maps"
BLUEMAP_URL = "http://127.0.0.1:8100"

PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
MC_ADDRESS = os.environ.get("MC_ADDRESS", "")
# Variables del contenedor que son solo del panel y no se le pasan a Minecraft.
PANEL_ENV = {"PANEL_PASSWORD", "SECRET_KEY", "MC_ADDRESS", "PANEL_PORT", "MC_PORT"}

UUID_RE = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$")
PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")
VERSION_RE = re.compile(r"^\d+(\.\d+){1,2}$")
USER_AGENT = "mc-tecnico-panel/1.0"

DIFFICULTIES = {"peaceful": "Pacífico", "easy": "Fácil", "normal": "Normal", "hard": "Difícil"}
GAMEMODES = {"survival": "Supervivencia", "creative": "Creativo", "adventure": "Aventura", "spectator": "Espectador"}
LEVEL_TYPES = {"minecraft:normal": "Normal", "minecraft:large_biomes": "Biomas grandes",
               "minecraft:amplified": "Amplificado", "minecraft:flat": "Plano"}
MAP_NAMES = {"minecraft:overworld": "Superficie", "minecraft:the_nether": "Nether", "minecraft:the_end": "El End"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30), MAX_CONTENT_LENGTH=1024 * 1024,
                  # Si no, cada imagen del mapa llevaría su propia cookie nueva.
                  SESSION_REFRESH_EACH_REQUEST=False)
if os.environ.get("PANEL_DEV"):
    app.config["TEMPLATES_AUTO_RELOAD"] = True


# ---------------------------------------------------------------- utilidades de red

def http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


_releases_cache = [0, []]


def mc_releases():
    """Versiones release de Minecraft, de la más nueva a la más vieja (según Mojang)."""
    if time.time() - _releases_cache[0] < 3600 and _releases_cache[1]:
        return _releases_cache[1]
    try:
        d = http_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        _releases_cache[:] = [time.time(), [v["id"] for v in d["versions"] if v["type"] == "release"]]
    except (OSError, ValueError, KeyError):
        pass
    return _releases_cache[1]


def fabric_supports(version):
    try:
        return bool(http_json(f"https://meta.fabricmc.net/v2/versions/loader/{urllib.parse.quote(version)}"))
    except (OSError, ValueError):
        return False


def pick_initial_version():
    """La versión más nueva de Minecraft para la que ya hay Fabric y todos los mods por defecto."""
    for v in mc_releases()[:4]:
        if not fabric_supports(v):
            continue
        avail = modrinth.availability(modrinth.DEFAULT_MODS, v)
        if all(avail.values()):
            return v
    return os.environ.get("MC_VERSION_INICIAL", "26.3")


def lookup_player(name):
    """Nombre exacto y UUID de una cuenta de Minecraft (para editar las listas con el servidor parado)."""
    for url in (f"https://api.mojang.com/users/profiles/minecraft/{name}",
                f"https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}"):
        try:
            d = http_json(url, timeout=8)
        except urllib.error.HTTPError as e:
            if e.code in (204, 404):
                return None
            continue
        except (OSError, ValueError):
            continue
        if d and d.get("id"):
            h = d["id"].replace("-", "")
            return d["name"], f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    raise RuntimeError("No se pudo consultar a Mojang. Prueba con el servidor en marcha.")


# ---------------------------------------------------------------- configuración

def default_config():
    return {
        "version": "",
        "mods": list(modrinth.DEFAULT_MODS),
        "memory_gb": 4,
        "motd": "Servidor técnico",
        "difficulty": "normal", "gamemode": "survival", "hardcore": False, "pvp": True,
        "max_players": 10, "view_distance": 10, "simulation_distance": 10,
        "whitelist": True, "allow_flight": True, "spawn_protection": 0, "command_blocks": False,
        "seed": "", "level_type": "minecraft:normal",
        "rcon_password": secrets.token_urlsafe(18),
        "map_public": False, "map_players": True,
        "backup_auto": True, "backup_hour": 5, "backup_keep": 7,
        "autostart": True,
    }


_config_lock = threading.Lock()


def load_config():
    cfg = default_config()
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"[panel] config.json ilegible, uso valores por defecto: {e}", flush=True)
    for slug in modrinth.CATALOG:
        if slug.get("required") and slug["slug"] not in cfg["mods"]:
            cfg["mods"].insert(0, slug["slug"])
    return cfg


def save_config(cfg):
    with _config_lock:
        PANEL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        tmp.chmod(0o600)
        tmp.replace(CONFIG_FILE)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(**changes):
    with _config_lock:
        st = load_state()
        st.update(changes)
        PANEL_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st))


def tf(v):
    return "TRUE" if v else "FALSE"


def minecraft_env(cfg):
    """Variables para la imagen de itzg: https://docker-minecraft-server.readthedocs.io"""
    return {
        "EULA": "TRUE", "TYPE": "FABRIC", "VERSION": cfg["version"],
        "MODRINTH_PROJECTS": ",".join(modrinth.spec(s) for s in cfg["mods"]),
        "MODRINTH_DOWNLOAD_DEPENDENCIES": "required", "MODRINTH_ALLOWED_VERSION_TYPE": "release",
        "INIT_MEMORY": "1G", "MAX_MEMORY": f"{cfg['memory_gb']}G", "USE_AIKAR_FLAGS": "TRUE",
        "ENABLE_RCON": "TRUE", "RCON_PORT": "25575", "RCON_PASSWORD": cfg["rcon_password"],
        "BROADCAST_RCON_TO_OPS": "FALSE", "SERVER_PORT": "25565", "ONLINE_MODE": "TRUE", "LEVEL": "world",
        "MOTD": cfg["motd"], "DIFFICULTY": cfg["difficulty"], "MODE": cfg["gamemode"],
        "HARDCORE": tf(cfg["hardcore"]), "PVP": tf(cfg["pvp"]), "MAX_PLAYERS": str(cfg["max_players"]),
        "VIEW_DISTANCE": str(cfg["view_distance"]), "SIMULATION_DISTANCE": str(cfg["simulation_distance"]),
        "ALLOW_FLIGHT": tf(cfg["allow_flight"]), "SPAWN_PROTECTION": str(cfg["spawn_protection"]),
        "ENABLE_COMMAND_BLOCK": tf(cfg["command_blocks"]), "ENABLE_WHITELIST": tf(cfg["whitelist"]),
        "SEED": cfg["seed"], "LEVEL_TYPE": cfg["level_type"],
        # En minúsculas: el script de itzg ejecuta el valor ("TRUE" falla y desactiva los logs).
        "ENABLE_ROLLING_LOGS": "true", "STOP_DURATION": "90", "ENABLE_AUTOPAUSE": "FALSE",
    }


def env_signature(cfg):
    return hashlib.sha1(json.dumps(minecraft_env(cfg), sort_keys=True).encode()).hexdigest()


def prepare_bluemap(cfg):
    """BlueMap no descarga las texturas ni renderiza hasta aceptar la descarga del cliente de Mojang
    (es parte del mismo EULA que ya se acepta para el servidor)."""
    BLUEMAP_CONF.mkdir(parents=True, exist_ok=True)
    core = BLUEMAP_CONF / "core.conf"
    if not core.exists():
        core.write_text("accept-download: true\nrender-thread-count: 2\nmetrics: false\n")
    else:
        text = core.read_text()
        if "accept-download: false" in text:
            core.write_text(text.replace("accept-download: false", "accept-download: true"))
    plugin = BLUEMAP_CONF / "plugin.conf"
    if plugin.exists():
        text = plugin.read_text()
        new = re.sub(r"(?m)^live-player-markers: *\w+", f"live-player-markers: {str(cfg['map_players']).lower()}", text)
        if new != text:
            plugin.write_text(new)
    for conf in (BLUEMAP_CONF / "maps").glob("*.conf"):
        text = conf.read_text()
        dim = re.search(r'(?m)^dimension: *"([^"]+)"', text)
        if dim and dim.group(1) in MAP_NAMES:
            new = re.sub(r'(?m)^name: *"world \([a-z_]+\)"', f'name: "{MAP_NAMES[dim.group(1)]}"', text)
            if new != text:
                conf.write_text(new)


def build_env():
    cfg = load_config()
    prepare_bluemap(cfg)
    env = {k: v for k, v in os.environ.items() if k not in PANEL_ENV}
    env.update(minecraft_env(cfg))
    return env, env_signature(cfg)


def note_activity():
    st = load_state()
    if time.time() - st.get("last_activity", 0) > 60:
        save_state(last_activity=time.time())


server = MCServer(DATA_DIR, build_env, load_config()["rcon_password"], on_activity=note_activity)


# ---------------------------------------------------------------- tareas largas (una a la vez)

job = {"name": "", "message": "", "last": None}
_job_lock = threading.Lock()


def run_job(name, fn):
    if not _job_lock.acquire(blocking=False):
        raise RuntimeError(f"Espera: ahora mismo estoy con «{job['name']}».")
    job.update(name=name, message=f"{name}…")

    def runner():
        ok, msg = True, ""
        try:
            msg = fn(lambda m: job.update(message=m)) or f"{name}: hecho."
        except Exception as e:
            ok, msg = False, f"{name}: {e}"
            server.panel_log(msg)
        finally:
            job.update(name="", message="", last={"ok": ok, "message": msg, "t": time.time()})
            _job_lock.release()

    threading.Thread(target=runner, daemon=True).start()


def stop_for_maintenance(report, reason):
    if server.running():
        report(f"Parando el servidor ({reason})…")
        server.announce(f"El servidor se va a parar: {reason}", 10)
        server.stop()


def purge_map_tiles():
    """Tras cambiar de mundo, BlueMap tiene que volver a dibujarlo todo (con el servidor parado).
    Además de las teselas se borra su estado: si no, cree que el mapa está al día (solo redibuja
    regiones modificadas después de su último repaso completo)."""
    if BLUEMAP_WEB_MAPS.is_dir():
        for d in BLUEMAP_WEB_MAPS.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
    for f in ("pluginState.json", "tasks.dat"):
        (DATA_DIR / "bluemap" / f).unlink(missing_ok=True)


# ---------------------------------------------------------------- web: acceso

_failed_logins = {}
_failed_total = []


def is_authed():
    return bool(session.get("auth"))


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_authed():
            if request.path.startswith("/api/") or request.method == "POST":
                abort(401)
            return redirect(url_for("login"))
        if request.method == "POST":
            token = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
            if not hmac.compare_digest(token, session.get("csrf", "")):
                abort(400)
        return view(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"csrf": session["csrf"]}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        now = time.time()
        # Detrás del túnel de Cloudflare todo llega desde la misma IP; además del límite por IP hay uno
        # global, por si alguien falsea la cabecera.
        ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or "?"
        recent = [t for t in _failed_logins.get(ip, []) if now - t < 300]
        _failed_total[:] = [t for t in _failed_total if now - t < 600]
        if len(recent) >= 5 or len(_failed_total) >= 30:
            flash("Demasiados intentos. Espera unos minutos.", "error")
            return render_template("login.html"), 429
        if PANEL_PASSWORD and hmac.compare_digest(request.form.get("password", "").encode(), PANEL_PASSWORD.encode()):
            _failed_logins.pop(ip, None)
            session.clear()
            session.permanent = True
            session["auth"] = True
            return redirect(url_for("index"))
        _failed_logins[ip] = recent + [now]
        _failed_total.append(now)
        time.sleep(1)
        flash("Contraseña incorrecta.", "error")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


def ok(message="", **extra):
    return jsonify(ok=True, message=message, **extra)


def fail(message, code=400):
    return jsonify(ok=False, message=message), code


def body():
    return request.get_json(silent=True) or request.form.to_dict()


# ---------------------------------------------------------------- web: páginas y estado

def address():
    return MC_ADDRESS or f"{request.host.split(':')[0]}:{os.environ.get('MC_PORT', '25565')}"


def fabric_loader():
    for p in DATA_DIR.glob("fabric-server-mc.*-loader.*.jar"):
        if m := re.search(r"mc\.(.+?)-loader\.(.+?)-launcher", p.name):
            return {"mc": m.group(1), "loader": m.group(2)}
    return {}


def settings_payload(cfg):
    keys = ("version", "memory_gb", "motd", "difficulty", "gamemode", "hardcore", "pvp", "max_players",
            "view_distance", "simulation_distance", "whitelist", "allow_flight", "spawn_protection",
            "command_blocks", "seed", "level_type", "map_public", "map_players", "backup_auto",
            "backup_hour", "backup_keep", "autostart", "mods")
    return {k: cfg[k] for k in keys}


def status_payload():
    cfg = load_config()
    snap = server.snapshot()
    all_backups = backups.list_backups()
    disk = shutil.disk_usage(DATA_DIR)
    snap.update({
        "pending": server.running() and server.applied_sig not in (None, env_signature(cfg)),
        "job": {"name": job["name"], "message": job["message"]},
        "job_last": job["last"],
        "events": list(server.events)[-40:],
        "log_n": server.log_n,
        "fabric": fabric_loader(),
        "settings": settings_payload(cfg),
        "last_backup": all_backups[0] if all_backups else None,
        "disk": {"free": disk.free, "total": disk.total},
        "mem_total": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
    })
    return snap


@app.get("/")
@login_required
def index():
    cfg = load_config()
    data = {
        "status": status_payload(), "address": address(),
        "difficulties": DIFFICULTIES, "gamemodes": GAMEMODES, "levelTypes": LEVEL_TYPES,
        "catalog": modrinth.CATALOG, "mapNames": MAP_NAMES,
        "memTotalGb": round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3),
    }
    return render_template("index.html", data=data, cfg=cfg)


@app.get("/api/status")
@login_required
def api_status():
    return jsonify(status_payload())


@app.get("/api/log")
@login_required
def api_log():
    after = request.args.get("after", 0, type=int)
    lines = [l for l in list(server.log) if l[0] > after][-1500:]
    return jsonify(lines=lines, n=server.log_n)


@app.post("/api/command")
@login_required
def api_command():
    cmd = str(body().get("cmd", "")).strip()
    if not cmd or len(cmd) > 1000 or "\n" in cmd:
        return fail("Escribe un comando.")
    if cmd.lstrip("/").split(" ")[0] in ("stop", "restart"):
        return fail("Para parar o reiniciar usa los botones del panel (guardan bien el mundo y avisan).")
    try:
        out = server.command(cmd)
    except RconError as e:
        return fail(f"No se pudo mandar: {e}")
    return ok(output=out if out is not None else "(enviado por la consola; la respuesta sale en el registro)")


@app.post("/server/<action>")
@login_required
def server_action(action):
    if action == "start":
        if server.running():
            return fail("El servidor ya está en marcha.")
        cfg = load_config()
        try:
            server.start()
        except OSError as e:
            return fail(f"No se pudo arrancar: {e}")
        if not cfg["autostart"]:
            cfg["autostart"] = True
            save_config(cfg)
        return ok("Arrancando el servidor…")
    if action not in ("stop", "restart"):
        abort(404)
    if not server.running():
        return fail("El servidor ya está parado.")

    def work(report):
        verb = "reiniciar" if action == "restart" else "parar"
        report(f"Avisando a los jugadores…" if server.players else f"Voy a {verb} el servidor…")
        server.announce(f"El servidor se va a {verb}", 10)
        report("Guardando el mundo y parando…")
        server.stop()
        cfg = load_config()
        if action == "restart":
            server.start()
            return "Reiniciando: tarda unos segundos en volver."
        cfg["autostart"] = False  # parado a mano: que no arranque solo al reiniciar el contenedor
        save_config(cfg)
        return "Servidor parado."
    try:
        run_job("Reiniciar" if action == "restart" else "Parar", work)
    except RuntimeError as e:
        return fail(str(e))
    return ok()


# ---------------------------------------------------------------- jugadores

def read_list(name):
    try:
        data = json.loads((DATA_DIR / name).read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def write_list(name, items):
    path = DATA_DIR / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2))
    tmp.replace(path)


@app.get("/api/players")
@login_required
def api_players():
    return jsonify(whitelist=read_list("whitelist.json"), ops=read_list("ops.json"),
                   bans=read_list("banned-players.json"), usercache=read_list("usercache.json")[:200])


PLAYER_ACTIONS = {
    "whitelist_add": ("whitelist add {name}", "whitelist.json"),
    "whitelist_remove": ("whitelist remove {name}", "whitelist.json"),
    "op": ("op {name}", "ops.json"),
    "deop": ("deop {name}", "ops.json"),
    "kick": ("kick {name} {reason}", None),
    "ban": ("ban {name} {reason}", "banned-players.json"),
    "pardon": ("pardon {name}", "banned-players.json"),
}


def offline_list_edit(action, name, reason):
    """Lo mismo que el comando, pero editando el JSON (con el servidor parado lo lee al arrancar)."""
    _, file = PLAYER_ACTIONS[action]
    items = read_list(file)
    lower = name.lower()
    exists = [i for i in items if i.get("name", "").lower() == lower]
    if action in ("whitelist_remove", "deop", "pardon"):
        if not exists:
            return f"{name} no está en la lista."
        write_list(file, [i for i in items if i.get("name", "").lower() != lower])
        return {"whitelist_remove": f"{name} ya no está en la lista blanca.", "deop": f"{name} ya no es op.",
                "pardon": f"{name} ya no está baneado."}[action]
    if exists:
        return f"{name} ya estaba en la lista."
    found = lookup_player(name)
    if not found:
        raise RuntimeError(f"No existe ninguna cuenta de Minecraft llamada «{name}».")
    real, uuid = found
    if action == "whitelist_add":
        items.append({"uuid": uuid, "name": real})
        msg = f"{real} añadido a la lista blanca."
    elif action == "op":
        items.append({"uuid": uuid, "name": real, "level": 4, "bypassesPlayerLimit": False})
        msg = f"{real} ahora es op."
    else:
        items.append({"uuid": uuid, "name": real, "created": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
                      "source": "Panel", "expires": "forever", "reason": reason or "Baneado desde el panel"})
        msg = f"{real} baneado."
    write_list(file, items)
    return msg


@app.post("/players/<action>")
@login_required
def player_action(action):
    if action not in PLAYER_ACTIONS:
        abort(404)
    d = body()
    name = str(d.get("name", "")).strip()
    reason = re.sub(r"[\r\n]", " ", str(d.get("reason", "")).strip())[:200]
    if not PLAYER_RE.match(name):
        return fail("Ese nombre de jugador no es válido (3–16 letras, números o _).")
    if server.state == "online":
        cmd = PLAYER_ACTIONS[action][0].format(name=name, reason=reason).strip()
        try:
            out = server.command(cmd)
        except RconError as e:
            return fail(f"No se pudo: {e}")
        return ok(out or "Hecho.")
    if action == "kick":
        return fail("El servidor está parado: no hay nadie a quien echar.")
    if server.running():
        return fail("Espera a que el servidor termine de arrancar.")
    try:
        return ok(offline_list_edit(action, name, reason))
    except RuntimeError as e:
        return fail(str(e))


@app.get("/skin/<uuid>")
@login_required
def skin(uuid):
    """La textura de la skin (la cara se recorta en el navegador). Se guarda un día."""
    uuid = uuid.lower()
    if not UUID_RE.match(uuid):
        abort(404)
    plain = uuid.replace("-", "")
    path = SKINS_DIR / f"{plain}.png"
    miss = SKINS_DIR / f"{plain}.none"
    fresh = lambda p: p.exists() and time.time() - p.stat().st_mtime < 86400
    if not fresh(path) and not fresh(miss):
        try:
            prof = http_json(f"https://sessionserver.mojang.com/session/minecraft/profile/{plain}", timeout=8)
            tex = next(p["value"] for p in prof["properties"] if p["name"] == "textures")
            url = json.loads(base64.b64decode(tex))["textures"]["SKIN"]["url"].replace("http://", "https://")
            if not url.startswith("https://textures.minecraft.net/"):
                raise ValueError(url)
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as r:
                png = r.read(256 * 1024)
            SKINS_DIR.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png)
        except (OSError, ValueError, KeyError, StopIteration, TypeError):
            if not path.exists():
                SKINS_DIR.mkdir(parents=True, exist_ok=True)
                miss.touch()
    if not path.exists():
        abort(404)
    resp = send_file(path, mimetype="image/png", max_age=86400)
    return resp


# ---------------------------------------------------------------- ajustes

def _int(d, key, lo, hi):
    try:
        v = int(d.get(key))
    except (TypeError, ValueError):
        raise ValueError(f"Valor no válido en {key}.")
    return max(lo, min(hi, v))


def _bool(d, key):
    return str(d.get(key, "")).lower() in ("1", "true", "on", "yes")


@app.post("/settings")
@login_required
def save_settings():
    d = body()
    cfg = load_config()
    mem_total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    try:
        motd = re.sub(r"[\r\n]+", " ", str(d.get("motd", ""))).strip()[:120] or "Servidor técnico"
        if d.get("difficulty") not in DIFFICULTIES or d.get("gamemode") not in GAMEMODES:
            raise ValueError("Dificultad o modo de juego no válidos.")
        # La semilla y el tipo de mundo solo cuentan al crear un mundo (se eligen en "Mundo nuevo").
        level_type = d.get("level_type", cfg["level_type"])
        if level_type not in LEVEL_TYPES:
            raise ValueError("Tipo de mundo no válido.")
        seed = re.sub(r"\s", "", str(d.get("seed", cfg["seed"])))[:64]
        cfg.update({
            "motd": motd, "difficulty": d["difficulty"], "gamemode": d["gamemode"],
            "hardcore": _bool(d, "hardcore"), "pvp": _bool(d, "pvp"),
            "max_players": _int(d, "max_players", 1, 200),
            "view_distance": _int(d, "view_distance", 3, 32),
            "simulation_distance": _int(d, "simulation_distance", 3, 32),
            "whitelist": _bool(d, "whitelist"), "allow_flight": _bool(d, "allow_flight"),
            "spawn_protection": _int(d, "spawn_protection", 0, 64),
            "command_blocks": _bool(d, "command_blocks"),
            "memory_gb": _int(d, "memory_gb", 1, max(2, int(mem_total) - 2)),
            "seed": seed, "level_type": level_type,
            "backup_auto": _bool(d, "backup_auto"), "backup_hour": _int(d, "backup_hour", 0, 23),
            "backup_keep": _int(d, "backup_keep", 1, 60),
        })
    except ValueError as e:
        return fail(str(e))
    save_config(cfg)
    pending = server.running() and server.applied_sig != env_signature(cfg)
    return ok("Guardado. Reinicia el servidor para aplicarlo." if pending else "Guardado.",
              settings=settings_payload(cfg), pending=pending)


# ---------------------------------------------------------------- mods y versión

@app.get("/api/mods")
@login_required
def api_mods():
    cfg = load_config()
    extra = [s for s in cfg["mods"] if s not in modrinth.BY_SLUG]
    installed = modrinth.installed(MODS_DIR)  # si Modrinth no responde, salen sin icono pero salen
    error = ""
    try:
        info = modrinth.projects(set(extra) | set(modrinth.BY_SLUG))
    except modrinth.ModrinthError as e:
        info, error = {}, str(e)
    icons = {s: info[s]["icon"] for s in modrinth.BY_SLUG if s in info}
    extras = [info.get(s, {"slug": s, "name": s, "summary": "", "icon": ""}) for s in extra]
    return jsonify(enabled=cfg["mods"], installed=installed, extras=extras, icons=icons, version=cfg["version"], error=error)


@app.get("/api/mods/search")
@login_required
def api_mods_search():
    q = request.args.get("q", "").strip()[:80]
    try:
        return jsonify(results=modrinth.search(q, load_config()["version"]))
    except modrinth.ModrinthError as e:
        return fail(str(e), 502)


@app.post("/mods/set")
@login_required
def mods_set():
    d = body()
    slug = str(d.get("slug", "")).strip()
    on = _bool(d, "on")
    if not SLUG_RE.match(slug):
        return fail("Mod no válido.")
    cfg = load_config()
    if modrinth.BY_SLUG.get(slug, {}).get("required"):
        return fail("Fabric API es obligatorio: lo necesitan los demás mods.")
    if on and slug not in cfg["mods"]:
        try:
            if not modrinth.best_version(slug, cfg["version"]):
                return fail(f"Ese mod no tiene versión para Fabric {cfg['version']}.")
        except modrinth.ModrinthError as e:
            return fail(str(e))
        cfg["mods"].append(slug)
    elif not on:
        cfg["mods"] = [s for s in cfg["mods"] if s != slug]
    save_config(cfg)
    pending = server.running() and server.applied_sig != env_signature(cfg)
    name = modrinth.BY_SLUG.get(slug, {}).get("name", slug)
    verb = "activado" if on else "quitado"
    return ok(f"{name} {verb}." + (" Se aplica al reiniciar." if pending else ""), enabled=cfg["mods"], pending=pending)


@app.get("/api/update")
@login_required
def api_update():
    cfg = load_config()
    releases = mc_releases()
    current = cfg["version"]
    newer = releases[:releases.index(current)] if current in releases else []
    target = request.args.get("version") or (newer[0] if newer else "")
    result = {"current": current, "latest": releases[0] if releases else "", "newer": newer, "target": target}
    if target and target in newer:
        result["fabric"] = fabric_supports(target)
        avail = modrinth.availability(cfg["mods"], target)
        result["mods"] = [{"slug": s, "name": modrinth.BY_SLUG.get(s, {}).get("name", s), "version": v}
                          for s, v in avail.items()]
    return jsonify(result)


@app.post("/update")
@login_required
def do_update():
    cfg = load_config()
    target = str(body().get("version", ""))
    releases = mc_releases()
    if not VERSION_RE.match(target) or target not in releases or cfg["version"] not in releases \
            or releases.index(target) >= releases.index(cfg["version"]):
        return fail("Solo se puede actualizar a una versión más nueva.")
    if not fabric_supports(target):
        return fail(f"Fabric todavía no tiene versión para {target}.")

    def work(report):
        if (DATA_DIR / "world").is_dir():
            report("Copia de seguridad antes de actualizar…")
            backups.create(DATA_DIR, "antes-de-actualizar", server, server.world_size, report)
        stop_for_maintenance(report, f"actualización a {target}")
        c = load_config()
        c["version"] = target
        save_config(c)
        server.start()
        return f"Actualizando a Minecraft {target}: el primer arranque tarda un poco más."
    try:
        run_job("Actualizar Minecraft", work)
    except RuntimeError as e:
        return fail(str(e))
    return ok()


# ---------------------------------------------------------------- copias y mundo

@app.get("/api/backups")
@login_required
def api_backups():
    disk = shutil.disk_usage(backups.BACKUP_DIR if backups.BACKUP_DIR.is_dir() else DATA_DIR)
    return jsonify(backups=backups.list_backups(), disk={"free": disk.free, "total": disk.total},
                   world_size=server.world_size)


@app.post("/backups/create")
@login_required
def backup_create():
    def work(report):
        name = backups.create(DATA_DIR, "manual", server, server.world_size, report)
        return f"Copia hecha: {name}"
    try:
        run_job("Copia de seguridad", work)
    except RuntimeError as e:
        return fail(str(e))
    return ok()


@app.post("/backups/delete")
@login_required
def backup_delete():
    try:
        backups.delete(str(body().get("name", "")))
    except backups.BackupError as e:
        return fail(str(e))
    return ok("Copia borrada.")


@app.get("/backups/download/<name>")
@login_required
def backup_download(name):
    try:
        path = backups.path_of(name)
    except backups.BackupError:
        abort(404)
    return send_file(path, as_attachment=True, download_name=name, mimetype="application/zstd")


@app.post("/backups/restore")
@login_required
def backup_restore():
    name = str(body().get("name", ""))
    try:
        backups.path_of(name)
    except backups.BackupError as e:
        return fail(str(e))

    def work(report):
        was_running = server.running()
        stop_for_maintenance(report, "se restaura una copia")
        if (DATA_DIR / "world").is_dir():
            report("Guardando el mundo actual por si acaso…")
            backups.create(DATA_DIR, "antes-de-restaurar", server, server.world_size, report)
        report("Restaurando la copia…")
        backups.restore(DATA_DIR, name)
        purge_map_tiles()
        server.world_size = None
        if was_running or load_config()["autostart"]:
            server.start()
        return "Copia restaurada. El mapa se vuelve a dibujar solo."
    try:
        run_job("Restaurar copia", work)
    except RuntimeError as e:
        return fail(str(e))
    return ok()


@app.post("/world/new")
@login_required
def world_new():
    d = body()
    seed = re.sub(r"\s", "", str(d.get("seed", "")))[:64]
    level_type = d.get("level_type", "minecraft:normal")
    if level_type not in LEVEL_TYPES:
        return fail("Tipo de mundo no válido.")

    def work(report):
        was_running = server.running()
        stop_for_maintenance(report, "se crea un mundo nuevo")
        if (DATA_DIR / "world").is_dir():
            report("Guardando una copia del mundo actual…")
            backups.create(DATA_DIR, "mundo-anterior", server, server.world_size, report)
            shutil.rmtree(DATA_DIR / "world")
        purge_map_tiles()
        server.world_size = None
        cfg = load_config()
        cfg.update(seed=seed, level_type=level_type)
        save_config(cfg)
        if was_running or cfg["autostart"]:
            server.start()
        return "Mundo nuevo creado. El anterior está en Copias (Mundo anterior)."
    try:
        run_job("Mundo nuevo", work)
    except RuntimeError as e:
        return fail(str(e))
    return ok()


def backup_scheduler():
    """Copia automática diaria a la hora elegida, si alguien ha jugado desde la anterior."""
    while True:
        time.sleep(60)
        try:
            cfg, st = load_config(), load_state()
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if not cfg["backup_auto"] or now.hour != cfg["backup_hour"] or st.get("last_auto_date") == today:
                continue
            if not (DATA_DIR / "world").is_dir() or job["name"]:
                continue
            last = backups.list_backups()
            if last and st.get("last_activity", 0) < last[0]["time"]:
                save_state(last_auto_date=today)  # nadie ha jugado: la última copia sigue valiendo
                continue

            def work(report, keep=cfg["backup_keep"]):
                name = backups.create(DATA_DIR, "auto", server, server.world_size, report)
                backups.prune(keep)
                return f"Copia automática hecha: {name}"
            save_state(last_auto_date=today)
            run_job("Copia automática", work)
        except Exception as e:
            print(f"[panel] copia automática: {e}", flush=True)


# ---------------------------------------------------------------- mapa (BlueMap)

@app.route("/mapa/", defaults={"path": ""})
@app.route("/mapa/<path:path>")
def map_proxy(path):
    """La web de BlueMap (puerto 8100 dentro del contenedor) servida a través del panel."""
    cfg = load_config()
    public = cfg["map_public"]
    if not public and not is_authed():
        return redirect(url_for("login"))
    if "bluemap" not in cfg["mods"]:
        return Response("BlueMap está desactivado en la pestaña Mods.", 404, mimetype="text/plain; charset=utf-8")
    # La conexión en directo (SSE) dejaría un hilo del panel ocupado por visitante; sin ella, BlueMap
    # pregunta cada poco por los jugadores, que funciona igual.
    if path.endswith("live/sse"):
        return Response("", 404)
    if ".." in path.split("/") or "\\" in path:
        abort(404)
    url = f"{BLUEMAP_URL}/{urllib.parse.quote(path)}"
    if request.query_string:
        url += "?" + request.query_string.decode("latin-1")
    headers = {"User-Agent": "panel"}
    for h in ("Accept-Encoding", "If-None-Match", "If-Modified-Since", "Accept"):
        if request.headers.get(h):
            headers[h] = request.headers[h]
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)
        status, content, rh = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        status, content, rh = e.code, e.read(), e.headers
    except OSError:
        msg = ("El mapa no está disponible: el servidor está parado o aún arrancando."
               if server.state != "online" else "BlueMap todavía está cargando. Prueba en unos segundos.")
        return Response(MAP_OFFLINE.replace("{msg}", msg), 503, mimetype="text/html; charset=utf-8")
    resp = Response(content, status)
    for h in ("Content-Type", "Content-Encoding", "ETag", "Last-Modified"):
        if rh.get(h):
            resp.headers[h] = rh[h]
    # Nada de caché pública: detrás de Cloudflare, un mapa privado acabaría guardado en su CDN.
    live = "/live/" in path or path.endswith(".json")
    resp.headers["Cache-Control"] = "no-cache" if live else "private, max-age=3600"
    return resp


MAP_OFFLINE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Mapa</title><body style="margin:0;display:grid;place-items:center;height:100vh;background:#0b0e0c;
color:#cfd8d2;font:16px system-ui;text-align:center;padding:20px"><div><div style="font-size:42px">🗺️</div>
<p>{msg}</p><p><a style="color:#5ee08a" href="">Reintentar</a></p></div>"""


@app.get("/api/map")
@login_required
def api_map():
    cfg = load_config()
    status = ""
    if server.state == "online" and "bluemap" in cfg["mods"]:
        try:
            status = server.command("bluemap", timeout=5)
        except RconError:
            pass
    return jsonify(public=cfg["map_public"], players=cfg["map_players"], enabled="bluemap" in cfg["mods"],
                   status=status, online=server.state == "online")


@app.post("/map/settings")
@login_required
def map_settings():
    d = body()
    cfg = load_config()
    if "public" in d:
        cfg["map_public"] = _bool(d, "public")
    if "players" in d:
        cfg["map_players"] = _bool(d, "players")
    save_config(cfg)
    prepare_bluemap(cfg)
    if "players" in d and server.state == "online":
        try:
            server.command("bluemap reload light", timeout=30)
        except RconError:
            pass
    return ok("Mapa público: cualquiera con el enlace puede verlo." if cfg["map_public"] and "public" in d
              else "Guardado.", public=cfg["map_public"], players=cfg["map_players"])


@app.post("/map/render")
@login_required
def map_render():
    full = _bool(body(), "full")
    try:
        out = server.command("bluemap force-update" if full else "bluemap update", timeout=15)
    except RconError as e:
        return fail(f"No se pudo: {e}")
    return ok("BlueMap está volviendo a dibujar todo el mundo." if full else "BlueMap está actualizando el mapa.",
              output=out)


# ---------------------------------------------------------------- arranque

def first_run():
    PANEL_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    if not cfg["version"]:
        cfg["version"] = pick_initial_version()
        print(f"[panel] versión de Minecraft elegida: {cfg['version']}", flush=True)
    if not CONFIG_FILE.exists() or not json.loads(CONFIG_FILE.read_text()).get("version"):
        save_config(cfg)
    return cfg


def shutdown(*_):
    server.stop()
    sys.exit(0)


if __name__ == "__main__":
    if not PANEL_PASSWORD:
        sys.exit("Falta PANEL_PASSWORD en el fichero .env")
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    cfg = first_run()
    server.rcon.password = cfg["rcon_password"]
    if cfg["autostart"]:
        server.start()
    threading.Thread(target=backup_scheduler, daemon=True).start()
    print("[panel] escuchando en :8000", flush=True)
    serve(app, host="0.0.0.0", port=8000, threads=16, ident=None)
