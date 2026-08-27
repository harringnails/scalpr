#!/bin/bash
set -u
set -o pipefail
umask 077

A2_WORKTREE="/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-a2-relabel"
A2_LIVE_ROOT="/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"

cd "$A2_WORKTREE" || exit 1
# launchd does not source interactive shell profiles. This loader reads the
# existing Keychain items without printing or persisting their values.
source "$A2_WORKTREE/load_keychain_env.sh"

exec "$A2_LIVE_ROOT/.venv/bin/python" \
  "$A2_WORKTREE/a2_relabel_nightly.py" \
  --worktree "$A2_WORKTREE" \
  --live-root "$A2_LIVE_ROOT"
