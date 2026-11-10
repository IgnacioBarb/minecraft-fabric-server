#!/usr/bin/env bash
# One-command setup. The first run creates .env (with a random panel password); every run then
# builds and starts the container. It never overwrites an existing .env, so it is safe to run again.
set -euo pipefail
cd "$(dirname "$0")"

GAME="Minecraft server"
ADDRESS_VAR="MC_ADDRESS"     # variable in .env that holds the address players connect to

die() { echo "✘ $*" >&2; exit 1; }
command -v docker >/dev/null || die "Docker is not installed: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing (the 'docker compose' command)."

random() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1" || true; }
env_get() { sed -n "s/^$1=//p" .env | head -n 1; }
env_set() {  # replaces NAME=... in .env, keeping comments and file permissions
  local tmp; tmp=$(mktemp)
  awk -v k="$1" -v v="$2" 'index($0, k "=") == 1 { $0 = k "=" v } { print }' .env >"$tmp"
  cat "$tmp" >.env; rm -f "$tmp"
}

if [[ ! -f .env ]]; then
  host=""
  if [[ -t 0 ]]; then
    echo "Address players will use to connect, e.g. myname.duckdns.org"
    read -rp "(press Enter to skip; you can change it later in .env): " host
  fi
  [[ "$host" =~ ^[A-Za-z0-9.-]*$ ]] || die "That does not look like a host name: $host"

  cp .env.example .env
  chmod 600 .env
  pass=$(random 16)
  env_set PANEL_PASSWORD "$pass"
  env_set SECRET_KEY "$(random 48)"

  if grep -q '^TZ=' .env; then  # use this machine's time zone for logs and daily backups
    tz=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || true)
    if [[ -n "$tz" ]]; then env_set TZ "$tz"; fi
  fi

  [[ -n "$host" ]] && env_set "$ADDRESS_VAR" "$host:$(env_get MC_PORT)" || env_set "$ADDRESS_VAR" ""
  echo "✔ Created .env"
  echo "  Panel password: $pass"
  echo
fi

docker compose up -d --build

port=$(env_get PANEL_PORT); port=${port:-8091}
ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "✔ $GAME is up."
echo "  Panel:    http://${ip:-localhost}:$port"
echo "  Password: PANEL_PASSWORD in .env"
echo "  The first start downloads Minecraft, Fabric and the mods (a few minutes)."
echo "  Port to open on your router: $(env_get MC_PORT) TCP (see README)."
