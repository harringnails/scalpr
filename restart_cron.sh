#!/bin/bash
# Headless Scalpr auto-restart (for cron or launchd). Unlike restart.sh this does
# NOT open a browser or hold a Terminal window — it kills any server on port 8420,
# then relaunches the server detached with both shadow observers enabled, logging
# to scalpr_server.log. Safe to run daily.
DIR="/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"

PID=$(lsof -tiTCP:8420 -sTCP:LISTEN)
if [ -n "$PID" ]; then
  HOLDING_COUNT=$(curl -fsS --max-time 5 http://127.0.0.1:8420/api/holdings 2>/dev/null | \
    python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("holdings", [])))' 2>/dev/null)
  if [ -z "$HOLDING_COUNT" ] || [ "$HOLDING_COUNT" -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S'): restart refused; holdings unavailable or nonzero" >> "$DIR/scalpr_autorestart.log"
    exit 1
  fi
  kill "$PID"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! lsof -tiTCP:8420 -sTCP:LISTEN >/dev/null; then break; fi
    sleep 1
  done
  if lsof -tiTCP:8420 -sTCP:LISTEN >/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S'): restart refused; server did not stop cleanly" >> "$DIR/scalpr_autorestart.log"
    exit 1
  fi
fi

cd "$DIR" || exit 1
. "$DIR/load_keychain_env.sh"
export WAVE_RIDING_ENABLED=1
export INCUBATION_SHADOW_ENABLED=1

nohup python3 scalp_server.py --sip > "$DIR/scalpr_server.log" 2>&1 &
echo "$(date '+%Y-%m-%d %H:%M:%S'): Scalpr auto-restarted with SIP (pid $!)" >> "$DIR/scalpr_autorestart.log"
