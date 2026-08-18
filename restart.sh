#!/bin/bash
# Safe restart for the Scalpr server.
# Run from anywhere:  bash "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/restart.sh"
# Frees port 8420 only if something is actually listening, then starts fresh.

PID=$(lsof -tiTCP:8420 -sTCP:LISTEN)
if [ -n "$PID" ]; then
  ACCOUNT_PROOF=$(curl -fsS --max-time 8 \
    http://127.0.0.1:8420/api/account-flat-proof 2>/dev/null)
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
    echo "REFUSING restart: the running server could not prove a flat active PAPER account."
    echo "Resolve the API issue first; do not force-kill a potentially guarded process."
    exit 1
  fi
  echo "Stopping verified-flat PAPER server on port 8420 (pid $PID)…"
  kill "$PID"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! lsof -tiTCP:8420 -sTCP:LISTEN >/dev/null; then break; fi
    sleep 1
  done
  if lsof -tiTCP:8420 -sTCP:LISTEN >/dev/null; then
    echo "Server did not stop cleanly; refusing to force-kill it."
    exit 1
  fi
else
  echo "Port 8420 is free."
fi

cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7" || exit 1

# Optional Unusual Whales token. The token stays in macOS Keychain and is never
# printed or stored in this repository.
. "./load_keychain_env.sh"

# Wave Riding shadow observer ON (Cohort A data collection).
# Shadow-only: no live orders, Standard mode unaffected. Delete this line to turn
# the observer back off — the rest of Scalpr is identical either way.
export WAVE_RIDING_ENABLED=1

# Entry Incubation shadow observer ON (standard-entry-incubation-shadow-cohort-a).
# Shadow-only, read-only, no live Guard change. Delete this line to turn it off.
# Runs in a bounded research worker, never in the Guard execution loop.
export INCUBATION_SHADOW_ENABLED=1

# Read-only provider capture. Both workers are isolated from Guard/order paths.
# UW is paced to one request/minute during the session; IVolatility captures one
# bounded SPY 0-2 DTE EOD chain after the close.
export SCALPR_UW_INGESTION_ENABLED=1
export SCALPR_IVOL_CAPTURE_ENABLED=1
export SCALPR_CLOUDSQL_MIRROR_ENABLED=1

# Entry Intelligence executable-bid collector ON for approved pre-lock capture.
# The code default remains OFF unless this exact flag is 1. It is read-only,
# paper/shadow-only, and all records remain formally cohort-ineligible.
export ENTRY_INTEL_BID_CAPTURE_ENABLED=1

# explain-v0 uses the deterministic brief/renderer only. A future external
# narration-plan adapter remains available in code, but is explicitly OFF here.
# No OpenAI/Claude credential is loaded or required by this server lifecycle.
export EXPLAIN_LAYER_ENABLED=0

echo "Starting Scalpr server with Alpaca SIP in the background…"
echo "Wave Riding shadow observer: ON (Cohort A)"
echo "Entry Incubation shadow observer: ON (Cohort A)"
echo "Unusual Whales shadow capture: ON (session only, 60s cadence)"
echo "IVolatility EOD capture: ON (SPY 0-2 DTE, once after close)"
echo "Entry executable-bid collector: ON (pre-lock dry-run; no order authority)"
echo "Evidence explanation: deterministic only (external AI adapter OFF)"
if [ "$SCALPR_UW_KEYCHAIN_STATUS" = "missing" ]; then
  echo "Unusual Whales Workup: OFF (token not found in environment or Keychain)"
else
  echo "Unusual Whales Workup: ON (credential source: $SCALPR_UW_KEYCHAIN_STATUS)"
fi
if [ "$SCALPR_IVOL_KEYCHAIN_STATUS" = "missing" ]; then
  echo "IVolatility Intelligence: OFF (API key not found in environment or Keychain)"
else
  echo "IVolatility Intelligence: ON (credential source: $SCALPR_IVOL_KEYCHAIN_STATUS; EOD capture only)"
fi

# Retire the optional mirror from the prior server lifecycle. The new server
# starts its replacement as an isolated no-broker subprocess.
CLOUDSQL_PID_FILE="v2_data/cloudsql_mirror.pid"
if [ -f "$CLOUDSQL_PID_FILE" ]; then
  CLOUDSQL_PID=$(sed -n '1p' "$CLOUDSQL_PID_FILE")
  CLOUDSQL_COMMAND=$(ps -p "$CLOUDSQL_PID" -o command= 2>/dev/null)
  case "$CLOUDSQL_COMMAND" in
    *cloudsql_mirror.py*--loop*) kill "$CLOUDSQL_PID" 2>/dev/null || true ;;
  esac
fi
if [ -x ".venv/bin/python" ] && [ -f "v2_data/cloudsql_profile.json" ]; then
  echo "Cloud SQL evidence mirror: ON (local files remain authoritative)"
else
  echo "Cloud SQL evidence mirror: OFF (optional runtime or profile missing)"
fi
ROOT_DIR=$(pwd -P)
LOG_DIR="$ROOT_DIR/v2_data"
SERVER_LOG="$LOG_DIR/scalpr_server.log"
SERVER_ERROR_LOG="$LOG_DIR/scalpr_server.error.log"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
mkdir -p "$LOG_DIR"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "REFUSING startup: project Python runtime is missing at $PYTHON_BIN."
  exit 1
fi

# The study collector must not depend on an interactive Terminal remaining open.
# It is intentionally launched by Terminal, which has the macOS privacy grant
# for the protected Documents folder. The inherited Keychain-only credentials
# remain in this process tree and are never written to disk.
/usr/bin/nohup "$PYTHON_BIN" scalp_server.py --sip \
  >>"$SERVER_LOG" 2>>"$SERVER_ERROR_LOG" < /dev/null &
SERVER_PID=$!
echo "Scalpr server launch requested (pid $SERVER_PID)."

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if lsof -tiTCP:8420 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Scalpr PAPER server is listening on http://127.0.0.1:8420."
    echo "Server log: $SERVER_LOG"
    exit 0
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Scalpr server exited during startup. Recent error output:"
    tail -n 40 "$SERVER_ERROR_LOG" 2>/dev/null || true
    exit 1
  fi
  sleep 1
done

echo "Scalpr server did not open port 8420 within 10 seconds. Recent error output:"
tail -n 40 "$SERVER_ERROR_LOG" 2>/dev/null || true
exit 1
