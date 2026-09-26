#!/usr/bin/env bash
# The server's Docker image and env file on localhost, with a separate data volume.
set -euo pipefail
ORIG_PWD=$PWD
cd "$(dirname "$0")"

PROJECT=househunt-local
COMPOSE_FILE=compose.yaml
[ -f "$COMPOSE_FILE" ] || COMPOSE_FILE=compose.example.yaml
compose() { docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"; }

usage() {
  cat <<EOF
Usage: ./run-local.sh [command]

  (none)            build and start in the foreground (Ctrl-C stops)
  up                build and start in the background
  down              stop
  logs              follow logs
  import FILE.yaml  create a saved search from a CLI config
  reset             stop and delete the local data volume
EOF
}

ensure_env() {
  if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example"
  fi
  if grep -q '^HOUSEHUNT_PASSWORD=change-me$' .env; then
    local pw
    pw=$(openssl rand -base64 12 | tr -d '/+=')
    sed -i.bak "s/^HOUSEHUNT_PASSWORD=change-me$/HOUSEHUNT_PASSWORD=$pw/" .env && rm -f .env.bak
    echo "Generated a password in .env: $pw"
  fi
  if ! grep -q '^DIGITRANSIT_API_KEY=.\+' .env; then
    echo "Note: DIGITRANSIT_API_KEY is empty in .env, so runs compute car times only."
  fi
}

banner() {
  local addr
  addr=$(compose port househunt 8000 2>/dev/null || true)
  echo
  echo "househunt: http://${addr:-localhost:8000}  (password: HOUSEHUNT_PASSWORD in .env)"
  echo
}

case "${1:-}" in
  "")
    ensure_env; banner
    compose up --build
    ;;
  up)
    ensure_env
    compose up --build -d
    banner
    ;;
  down)
    compose down
    ;;
  logs)
    compose logs -f
    ;;
  import)
    [ -n "${2:-}" ] || { echo "import needs a YAML file" >&2; exit 2; }
    src=$(cd "$ORIG_PWD" && cd "$(dirname "$2")" 2>/dev/null && pwd)/$(basename "$2") || true
    [ -f "$src" ] || { echo "No such file: $2" >&2; exit 2; }
    ensure_env
    name=$(basename "$src")
    compose run --rm -v "$src:/imports/$name:ro" \
      househunt python -m househunt web --import-config "/imports/$name"
    ;;
  reset)
    compose down -v
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage; exit 2
    ;;
esac
