#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$ROOT/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "Missing project Python: $PYTHON" >&2
  exit 1
fi

exec caffeinate -i "$PYTHON" "$ROOT/market_context_readonly_server_v0.py" serve \
  --ledger "$ROOT/market_context_shadow_v0.jsonl" \
  --host 127.0.0.1 \
  --port 8421
