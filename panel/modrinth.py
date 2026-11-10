"""Mods de Modrinth: el catálogo recomendado, búsquedas y qué versión hay para cada Minecraft.

Quien descarga los mods es la imagen de itzg (MODRINTH_PROJECTS); aquí solo se decide la lista
y se consulta la API para enseñar información en el panel.
"""

import hashlib
import json
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

API = "https://api.modrinth.com/v2"
USER_AGENT = "mc-tecnico-panel/1.0 (servidor casero con itzg/minecraft-server)"

# group: base (obligatorio), optim (rendimiento), tools (herramientas), map (mapa web).
# channel: el tipo de versión más inestable que se acepta. default: viene activado al crear el servidor.
CATALOG = [
    {"slug": "fabric-api", "name": "Fabric API", "group": "base", "required": True, "default": True,
     "desc": "La librería que necesitan casi todos los mods de Fabric."},
    {"slug": "lithium", "name": "Lithium", "group": "optim", "default": True,
     "desc": "Optimiza la física, la IA de los mobs, las tolvas, los bloques y el tick del mundo. "
             "Las granjas y la redstone funcionan igual que en vanilla: es el estándar de los servidores técnicos."},
    {"slug": "ferrite-core", "name": "FerriteCore", "group": "optim", "default": True,
     "desc": "Reduce mucho el uso de memoria RAM sin tocar nada del juego."},
    {"slug": "krypton", "name": "Krypton", "group": "optim", "default": True,
     "desc": "Optimiza la red: menos CPU y menos ancho de banda por jugador conectado."},
    {"slug": "spark", "name": "spark", "group": "tools", "default": True,
     "desc": "Medidor de rendimiento: con /spark profiler encuentras qué granja o qué chunk da lag. No cambia nada del juego."},
    {"slug": "bluemap", "name": "BlueMap", "group": "map", "default": True,
     "desc": "El mapa 3D del mundo que ves en la pestaña Mapa. Solo lee el mundo; no cambia nada."},
    {"slug": "chunky", "name": "Chunky", "group": "tools", "default": False,
     "desc": "Pregenera el mundo por adelantado (/chunky radius 3000, /chunky start). Menos lag al explorar "
             "y el mapa sale completo."},
    {"slug": "carpet", "name": "Carpet", "group": "tools", "default": False,
     "desc": "La navaja suiza técnica: jugadores falsos (/player) para mantener granjas cargadas, contadores de "
             "tolvas, /log, /spawn… Por sí solo no cambia nada: las reglas se activan a mano con /carpet."},
    {"slug": "servux", "name": "Servux", "group": "tools", "default": False,
     "desc": "Para quien use MiniHUD o Litematica: les manda datos del servidor (estructuras, zonas de spawn…)."},
    {"slug": "syncmatica", "name": "Syncmatica", "group": "tools", "default": False,
     "desc": "Comparte esquemas de Litematica entre los jugadores del servidor."},
    {"slug": "c2me-fabric", "name": "C2ME", "group": "optim", "default": False, "channel": "alpha",
     "desc": "Genera y carga chunks usando varios núcleos. Solo existe en versión alpha: úsalo si exploráis "
             "mucho y vigila que todo vaya bien."},
]
BY_SLUG = {m["slug"]: m for m in CATALOG}
DEFAULT_MODS = [m["slug"] for m in CATALOG if m["default"]]
CHANNEL_RANK = {"release": 0, "beta": 1, "alpha": 2}

_cache = {}
_cache_lock = threading.Lock()


class ModrinthError(Exception):
    pass


def _get(path, params=None, ttl=600, data=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (list, dict)) else v
                                             for k, v in params.items()})
    key = (url, json.dumps(data, sort_keys=True) if data else None)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            result = None
        else:
            raise ModrinthError(f"Modrinth respondió {e.code}") from e
    except (OSError, ValueError) as e:
        raise ModrinthError(f"No se pudo consultar Modrinth: {e}") from e
    with _cache_lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (time.time(), result)
    return result


def spec(slug):
    """Cómo se le pasa un mod a itzg: fabric:<slug>?:<canal>. El '?' hace que, si un mod no tiene versión
    para este Minecraft, se salte con un aviso en vez de impedir que arranque el servidor."""
    channel = BY_SLUG.get(slug, {}).get("channel", "release")
    optional = "" if BY_SLUG.get(slug, {}).get("required") else "?"
    return f"fabric:{slug}{optional}" + (f":{channel}" if channel != "release" else "")


def best_version(slug, game_version):
    """La versión que bajaría itzg para este Minecraft, o None si no hay."""
    channel = BY_SLUG.get(slug, {}).get("channel", "release")
    versions = _get(f"/project/{urllib.parse.quote(slug)}/version",
                    {"loaders": ["fabric"], "game_versions": [game_version]}) or []
    ok = [v for v in versions if CHANNEL_RANK.get(v["version_type"], 9) <= CHANNEL_RANK[channel]]
    if not ok:
        return None
    v = ok[0]
    return {"version": v["version_number"], "type": v["version_type"], "date": v["date_published"][:10]}


def availability(slugs, game_version):
    out = {}
    for slug in slugs:
        try:
            out[slug] = best_version(slug, game_version)
        except ModrinthError:
            out[slug] = "?"
    return out


def projects(slugs):
    """Nombre, descripción e icono de varios proyectos de una vez."""
    if not slugs:
        return {}
    found = _get("/projects", {"ids": sorted(slugs)}, ttl=3600) or []
    out = {}
    for p in found:
        info = {"slug": p["slug"], "id": p["id"], "name": p["title"], "summary": p["description"],
                "icon": p.get("icon_url") or "", "client_side": p.get("client_side"),
                "server_side": p.get("server_side"), "downloads": p.get("downloads", 0)}
        out[p["slug"]] = out[p["id"]] = info
    return out


def search(query, game_version, limit=20):
    facets = [["categories:fabric"], [f"versions:{game_version}"], ["project_type:mod"],
              ["server_side:required", "server_side:optional"]]
    result = _get("/search", {"query": query, "facets": facets, "limit": limit, "index": "relevance"}, ttl=300)
    return [{"slug": h["slug"], "name": h["title"], "summary": h["description"], "icon": h.get("icon_url") or "",
             "downloads": h.get("downloads", 0), "client_side": h.get("client_side"),
             "server_side": h.get("server_side")} for h in (result or {}).get("hits", [])]


# ---------------------------------------------------------------- mods instalados

def _jar_info(path):
    """id, nombre y versión que declara el propio .jar (fabric.mod.json)."""
    try:
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read("fabric.mod.json").decode("utf-8", "replace"), strict=False)
        return {"modid": meta.get("id", ""), "name": meta.get("name") or meta.get("id", path.stem),
                "version": str(meta.get("version", ""))}
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return {"modid": "", "name": path.stem, "version": ""}


_installed_cache = {}


def installed(mods_dir: Path):
    """Los .jar de la carpeta mods, con su proyecto de Modrinth si se reconoce (por el hash del fichero)."""
    jars = sorted(p for p in mods_dir.glob("*.jar") if p.is_file()) if mods_dir.is_dir() else []
    items = []
    for jar in jars:
        st = jar.stat()
        key = (jar.name, st.st_size, int(st.st_mtime))
        if key not in _installed_cache:
            info = _jar_info(jar)
            info["sha1"] = hashlib.sha1(jar.read_bytes()).hexdigest()
            _installed_cache[key] = info
        items.append({"file": jar.name, "size": st.st_size, **_installed_cache[key]})
    hashes = [i["sha1"] for i in items]
    try:
        versions = _get("/version_files", data={"hashes": hashes, "algorithm": "sha1"}, ttl=3600) if hashes else {}
        info = projects({v["project_id"] for v in (versions or {}).values()})
    except ModrinthError:
        versions, info = {}, {}
    for i in items:
        v = (versions or {}).get(i["sha1"])
        p = info.get(v["project_id"]) if v else None
        i["slug"] = p["slug"] if p else ""
        i["icon"] = p["icon"] if p else ""
        if p:
            i["name"] = p["name"]
        if v:
            i["version"] = v["version_number"]
        i.pop("sha1", None)
    return items
