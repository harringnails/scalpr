#!/bin/bash
# Headless restart candidate for cron/launchd. This file is intentionally not
# installed or scheduled by the repository. It fails closed unless the running
# server proves an active, flat PAPER account with no open orders.
set -u

DIR="/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
LOG="$DIR/scalpr_autorestart.log"

record() {
  printf '%s: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG"
}

PIDS=$(lsof -tiTCP:8420 -sTCP:LISTEN 2>/dev/null || true)
PID_COUNT=$(printf '%s\n' "$PIDS" | awk 'NF { count++ } END { print count+0 }')
if [ "$PID_COUNT" -ne 1 ]; then
  record "restart refused; expected exactly one server on port 8420, found $PID_COUNT"
  exit 1
fi
PID=$(printf '%s\n' "$PIDS" | awk 'NF { print; exit }')

ACCOUNT_PROOF=$(curl -fsS --max-time 8 \
  http://127.0.0.1:8420/api/account-flat-proof 2>/dev/null || true)
FLAT_VERDICT=$(printf '%s' "$ACCOUNT_PROOF" | python3 -c '
import json, sys
try:
    proof = json.load(sys.stdin)
    ok = (
        proof.get("source") == "alpaca_trading_api_direct_uncached"
        and proof.get("mode") == "paper"
        and str(proof.get("account_status", "")).upper() == "ACTIVE"
        and proof.get("flat") is True
        and proof.get("positions_count") == 0
        and proof.get("open_orders_count") == 0
    )
    print("OK" if ok else "BLOCK")
except Exception:
    print("UNVERIFIED")
' 2>/dev/null)
if [ "$FLAT_VERDICT" != "OK" ]; then
  record "restart refused; direct uncached active PAPER flat proof failed"
  exit 1
fi

kill "$PID"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if ! lsof -tiTCP:8420 -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if lsof -tiTCP:8420 -sTCP:LISTEN >/dev/null 2>&1; then
  record "restart refused; server did not stop cleanly"
  exit 1
fi

cd "$DIR" || exit 1
. "$DIR/load_keychain_env.sh"
export WAVE_RIDING_ENABLED=1
export INCUBATION_SHADOW_ENABLED=1
export SCALPR_UW_INGESTION_ENABLED=0
export SCALPR_IVOL_CAPTURE_ENABLED=0
export SCALPR_CLOUDSQL_MIRROR_ENABLED=1
export ENTRY_INTEL_BID_CAPTURE_ENABLED=1
export EXPLAIN_LAYER_ENABLED=0

# Retire only the tracked process whose command identifies the mirror loop.
CLOUDSQL_PID_FILE="v2_data/cloudsql_mirror.pid"
if [ -f "$CLOUDSQL_PID_FILE" ]; then
  CLOUDSQL_PID=$(sed -n '1p' "$CLOUDSQL_PID_FILE")
  CLOUDSQL_COMMAND=$(ps -p "$CLOUDSQL_PID" -o command= 2>/dev/null || true)
  case "$CLOUDSQL_COMMAND" in
    *cloudsql_mirror.py*--loop*) kill "$CLOUDSQL_PID" 2>/dev/null || true ;;
  esac
fi

PYTHON="python3"
if [ -x "$DIR/.venv/bin/python" ]; then
  PYTHON="$DIR/.venv/bin/python"
fi
nohup "$PYTHON" scalp_server.py --sip > "$DIR/scalpr_server.log" 2>&1 &
NEW_PID=$!

for _ in 1 2 3 4 5 6 7 8 9 10; do
  COLLECTOR_STATUS=$(curl -fsS --max-time 2 \
    http://127.0.0.1:8420/api/entry-intelligence/collector 2>/dev/null || true)
  COLLECTOR_VERDICT=$(printf '%s' "$COLLECTOR_STATUS" | python3 -c '
import json, sys
try:
    status = json.load(sys.stdin)
    ok = (
        status.get("collector_version") == "entry-bid-collector-v1.2"
        and status.get("config_version") == "entry-intelligence-config-v1.2.0"
        and status.get("enabled") is True
        and status.get("collection_role") == "PRELOCK_DRY_RUN"
        and status.get("cohorts_locked") is False
        and status.get("execution_authority") is False
        and status.get("guard_access") is False
    )
    print("OK" if ok else "BLOCK")
except Exception:
    print("UNVERIFIED")
' 2>/dev/null)
  if [ "$COLLECTOR_VERDICT" = "OK" ]; then
    record "Scalpr auto-restarted with verified v1.2 PRELOCK collector (pid $NEW_PID)"
    exit 0
  fi
  sleep 1
done
record "restart failed verification; v1.2 PRELOCK safety status not proven (pid $NEW_PID)"
exit 1
