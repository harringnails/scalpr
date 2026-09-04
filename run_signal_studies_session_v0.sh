#!/bin/bash
# Operator-installed shadow-study launcher. No server or trading process is started.
set -u

ROOT="/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
PYTHON="$ROOT/.venv/bin/python"

cd "$ROOT" || exit 1
. "$ROOT/load_keychain_env.sh"

exec /usr/bin/caffeinate -i "$PYTHON" "$ROOT/signal_study_session_runner_v0.py" run-session
