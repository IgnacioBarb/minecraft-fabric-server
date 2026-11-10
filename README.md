# Minecraft server + web panel + 3D map

A Minecraft Java server with **Fabric** and performance mods that don't change anything about vanilla
gameplay, a password-protected **web panel** to run it, and a **3D map** of the world (BlueMap) to see
everyone's builds.

Everything runs in a single container: the panel (Python/Flask) starts and stops Minecraft using the
[itzg/minecraft-server](https://docker-minecraft-server.readthedocs.io) image underneath, which
downloads Minecraft, Fabric and the mods from Modrinth.

> The panel's interface is in Spanish for now.

## Quick start

You need a Linux machine with [Docker](https://docs.docker.com/engine/install/) (Compose v2).

```bash
git clone https://github.com/IgnacioBarb/minecraft-fabric-server.git
cd minecraft-fabric-server
./setup.sh
```

`setup.sh` creates `.env` with a random panel password (it prints it), asks for the address your
friends will use, and starts the container. Running it again only rebuilds and restarts; it never
touches an existing `.env`.

Panel: `http://SERVER_IP:8091`

The first start picks the newest Minecraft version that already has Fabric and all the performance
mods (26.3 at the time of writing) and pins it. Updates are done from the **Mods** tab, which first
checks which mods have a matching version and backs up the world.

<details><summary>Manual setup (without setup.sh)</summary>

```bash
cp .env.example .env     # set PANEL_PASSWORD and MC_ADDRESS
docker compose up -d --build
```
</details>

## How friends join

The game listens on port **25565** (`MC_PORT` in `.env`; change it if you already run another
Minecraft server on that port). Two options:

- **Router**: forward `MC_PORT` (TCP) to the server machine; friends join with
  `your-domain.duckdns.org:25565`.
- **[playit.gg](https://playit.gg)** (no router access needed): add a Minecraft Java tunnel to
  `SERVER_IP:25565` and put the address it gives you in `MC_ADDRESS` in `.env`.

The address shown in the panel is `MC_ADDRESS` (after changing it: `docker compose up -d`).

## The panel

| Tab | What's there |
|---|---|
| Home (*Inicio*) | Status, address, TPS/MSPT with a chart of the last hour, RAM and CPU, world day, activity (joins, chat, advancements, deaths) |
| Players (*Jugadores*) | Online players (with "show on map"), whitelist, operators and bans. Works with the server stopped too |
| Map (*Mapa*) | BlueMap inside the panel. Toggles to make it **public** (`/mapa/` without password) and to hide players |
| Console (*Consola*) | Live log, commands (over RCON, with replies) and shortcuts: save, make it day, find lag with spark… |
| Mods | Recommended mods with on/off switches, Modrinth search, installed files and Minecraft updates |
| Settings (*Ajustes*) | server.properties (difficulty, distances, RAM…), backups and "new world" |

### Mods enabled by default

- **Lithium** – optimises physics, mob AI, hoppers and world ticking. Keeps vanilla behaviour.
- **FerriteCore** – less RAM.
- **Krypton** – more efficient networking.
- **spark** – performance monitor and profiler (`/spark profiler`).
- **BlueMap** – the map.
- **Fabric API** – library the others need.

Optional in the Mods tab: Carpet, Chunky, Servux, Syncmatica and C2ME (alpha). They are all
server-side: players join with the normal Minecraft client.

## Backups

In `backups/` (one `.tar.zst` per backup). Automatic every day at the chosen time if someone has
played; the last 7 automatic ones are kept. Restoring, updating or creating a new world makes another
backup first, which is never deleted automatically.

Restore by hand with the container stopped:

```bash
docker compose stop
rm -rf data/world
tar -I zstd -xf backups/<backup>.tar.zst -C data world
docker compose start
```

Backups only live on this machine (they are not in git): copy `backups/` somewhere else if you want to
be able to move the server to another machine with its world.

## Updating

```bash
git pull
docker compose up -d --build
```

## Files

- `data/` – the server (world in `data/world`, mods, config). The panel's settings are in `data/panel/config.json`.
- `data/config/bluemap/` – map config; the rendered map tiles are in `data/bluemap/`.
- `backups/` – backups.
- `panel/` – the panel code (Flask). After changing it: `docker compose up -d --build`.

`docker compose stop` saves the world and shuts Minecraft down (it can take up to a minute).

---

The panel icons are the game's official ones (© Mojang), used only to identify the game.
