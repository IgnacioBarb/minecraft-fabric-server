"""Copias de seguridad del mundo: un .tar.zst por copia en /backups."""

import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from rcon import RconError

BACKUP_DIR = Path("/backups")
NAME_RE = re.compile(r"^(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)_([a-z-]+)\.tar\.zst$")
KINDS = {"auto": "Automática", "manual": "Manual", "antes-de-restaurar": "Antes de restaurar",
         "antes-de-actualizar": "Antes de actualizar", "mundo-anterior": "Mundo anterior"}
# Además del mundo se guardan la lista blanca, los ops y los baneos (al restaurar solo se recupera el mundo).
EXTRA_FILES = ("server.properties", "whitelist.json", "ops.json", "banned-players.json", "banned-ips.json")


class BackupError(Exception):
    pass


def list_backups():
    out = []
    if BACKUP_DIR.is_dir():
        for p in BACKUP_DIR.iterdir():
            m = NAME_RE.match(p.name)
            if not m or not p.is_file():
                continue
            ts = datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S").timestamp()
            out.append({"name": p.name, "kind": m.group(2), "label": KINDS.get(m.group(2), m.group(2)),
                        "time": round(ts), "size": p.stat().st_size})
    return sorted(out, key=lambda b: b["time"], reverse=True)


def path_of(name):
    if not NAME_RE.match(name or ""):
        raise BackupError("Esa copia no existe.")
    p = BACKUP_DIR / name
    if not p.is_file():
        raise BackupError("Esa copia no existe.")
    return p


def create(data_dir: Path, kind, server, world_size=None, report=lambda msg: None):
    """Hace una copia. Con el servidor en marcha se desactiva el guardado mientras se copia."""
    world = data_dir / "world"
    if not world.is_dir():
        raise BackupError("Todavía no hay mundo que copiar.")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    need = (world_size or 0) * 0.8 + 200 * 1024 ** 2
    if shutil.disk_usage(BACKUP_DIR).free < need:
        raise BackupError("No queda espacio en el disco para la copia.")
    name = f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{kind}.tar.zst"
    final, part = BACKUP_DIR / name, BACKUP_DIR / (name + ".part")
    online = server.state == "online"
    if online:
        report("Guardando el mundo…")
        try:
            server.command("save-off")
            server.command("save-all flush", timeout=600)
        except RconError as e:
            _save_on(server, online)
            raise BackupError(f"No se pudo guardar el mundo antes de copiarlo: {e}") from e
    try:
        report("Comprimiendo el mundo…")
        members = ["world"] + [f for f in EXTRA_FILES if (data_dir / f).is_file()]
        r = subprocess.run(["tar", "-I", "zstd -T0 -3 -q", "-cf", str(part), "-C", str(data_dir),
                            "--exclude=world/session.lock", *members], capture_output=True, text=True)
        # 1 = "algún fichero cambió mientras se leía": con el guardado desactivado no debería pasar.
        if r.returncode not in (0, 1):
            part.unlink(missing_ok=True)
            raise BackupError(f"tar falló: {r.stderr.strip()[-300:]}")
        part.rename(final)
    finally:
        _save_on(server, online)
    return name


def _save_on(server, online):
    if online:
        for _ in range(3):
            try:
                server.command("save-on")
                return
            except RconError:
                time.sleep(2)


def restore(data_dir: Path, name):
    """Sustituye el mundo por el de la copia (con el servidor parado)."""
    src = path_of(name)
    world = data_dir / "world"
    tmp = data_dir / "world.restaurando"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    r = subprocess.run(["tar", "-I", "zstd -q", "-xf", str(src), "-C", str(tmp), "world"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not (tmp / "world").is_dir():
        shutil.rmtree(tmp, ignore_errors=True)
        raise BackupError(f"No se pudo descomprimir la copia: {r.stderr.strip()[-300:]}")
    shutil.rmtree(world, ignore_errors=True)
    (tmp / "world").rename(world)
    tmp.rmdir()


def delete(name):
    path_of(name).unlink()


def prune(keep):
    """Deja solo las `keep` copias automáticas más recientes; las demás se borran a mano."""
    autos = [b for b in list_backups() if b["kind"] == "auto"]
    for b in autos[keep:]:
        (BACKUP_DIR / b["name"]).unlink(missing_ok=True)
