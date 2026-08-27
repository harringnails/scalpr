#!/bin/bash
set -u
set -o pipefail
umask 077

A2_WORKTREE="/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-a2-relabel"
A2_LIVE_ROOT="/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"

cd "$A2_WORKTREE" || exit 1
# Manual compatibility entry point only. The Python process performs a strict,
# bounded Keychain read and does not accept inherited credentials as proof.
exec "$A2_LIVE_ROOT/.venv/bin/python" \
  "$A2_WORKTREE/a2_relabel_nightly.py" \
  --worktree "$A2_WORKTREE" \
  --live-root "$A2_LIVE_ROOT"
