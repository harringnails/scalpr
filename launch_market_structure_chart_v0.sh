#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$ROOT/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "Missing project Python: $PYTHON" >&2
  exit 1
fi

. "$ROOT/load_keychain_env.sh"

exec caffeinate -i "$PYTHON" "$ROOT/market_structure_readonly_server_v0.py" serve \
  --host 127.0.0.1 --port 8423 \
  --structure-ledger "$ROOT/multi_instrument_flashalpha_v0.jsonl" \
  --multi-episodes "$ROOT/multi_instrument_signal_v0.jsonl" \
  --spy-a "$ROOT/prior_regime_flip_reclaim_v0.jsonl" \
  --spy-b "$ROOT/intraday_continuation_v0.jsonl" \
  --echarts "$ROOT/vendor/echarts.min.js"
