#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ "$#" -eq 0 ]; then
  exec .venv/bin/python scripts/autonomous.py
fi
if [ "${1:-}" = "setup" ]; then
  exec .venv/bin/python scripts/autonomous.py setup
fi
if [ "${1:-offline}" = "daily" ]; then
  shift
  [ "$#" -eq 2 ] || { echo "Usage: ./start.sh daily /private/permit.json SHA256"; exit 2; }
  exec .venv/bin/python scripts/daily_launcher.py "$1" "$2"
fi
if [ "${1:-offline}" = "live" ]; then
  shift
  [ "$#" -eq 2 ] || { echo "Usage: ./start.sh live /private/permit.json SHA256"; exit 2; }
  exec .venv/bin/python "$PWD/scripts/mac_live.py" "$1" "$2"
fi
exec .venv/bin/python scripts/mac_local.py start "$@"
