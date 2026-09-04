#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$ROOT/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "Missing project Python: $PYTHON" >&2
  exit 1
fi

. "$ROOT/load_keychain_env.sh"
exec caffeinate -i "$PYTHON" "$ROOT/option_scenario_readonly_server_v0.py" serve \
  --host 127.0.0.1 --port 8422
