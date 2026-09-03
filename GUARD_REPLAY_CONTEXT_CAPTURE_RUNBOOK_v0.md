# Guard Replay + Context Capture Runbook v0

Both tools are paper/shadow research utilities. They have no execution, admission, order, collector, A2, dense-store, or live-Guard path.

## Reproduce the 2026-09-03 Guard replay

```bash
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
.venv/bin/python guard_operability_replay_v0.py run --session-date 2026-09-03
```

This deterministically replaces `guard_operability_counterfactual_v0.jsonl`. The file is gitignored evidence and is hashable/repeatable.

## Start forward market-context capture

Run during RTH after the separate FlashAlpha pin scanner is running:

```bash
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
source ./load_keychain_env.sh
caffeinate -i .venv/bin/python market_context_shadow_v0.py capture \
  --until-close \
  --interval-seconds 60 \
  --flashalpha-ledger "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-flashalpha-pin-scanner/flashalpha_pin_study_v0.jsonl"
```

The sampler appends only to gitignored `market_context_shadow_v0.jsonl`. It must start during 09:30–16:00 America/New_York and `--until-close` stops it cleanly at the session boundary regardless of its start time. Missing or stale FlashAlpha observations remain missing/stale; they are never substituted. The Alpaca credentials come from the existing macOS Keychain loader and are never printed or written.

The FlashAlpha ledger retains its native five-minute cadence. Repeating its latest observation at the context sampler's one-minute cadence does not make it one-minute data: every field retains the original provider timestamp and becomes degraded/stale at the frozen freshness limits.
