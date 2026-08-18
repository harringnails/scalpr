# Sync Restart + Hold-the-Runner Checklist

**Status:** operational checklist. Paper/shadow. Operator-run restart; Codex does the pre-restart code + post-restart confirmation. No cohort lock, no threshold change.

## Why this exists

The running server is behind disk. Confirmed live today: regime **v0.1** is active (15/15 episodes FRESH) and rollover holds. **Not** live yet: the **dense-source A2 fix** (not implemented) and the **runner measurement layer** (shadow pool doesn't exist). Because regime v0.1 is live, the regime-flow **runner can now activate** — so the measurement layer must be running *before* the runner is ever enabled, or an activation goes unmeasured. This checklist syncs running→disk and enforces that ordering.

## The one rule that overrides everything

**Do NOT enable the runner opt-in until the measurement layer is confirmed active (step 10).** Regime v0.1 makes the runner able to act on a still-unvalidated signal; without the paired shadow record live, that action is untracked — the exact failure the measurement was built to prevent. Runner stays OFF until measured.

## A — Pre-restart (Codex, autonomous)

1. Implement the dense-source A2 fix per `A2_DENSE_ENDPOINT_SOURCE_SPEC_v0.md`; tests green.
2. Confirm committed-on-disk and passing in one `.venv` pytest run (report the count): A2-accrual-independent, regime v0.1, runner measurement, dense-source.
3. Confirm the runner opt-in **default is OFF** in config, so the restart does not bring up an enabled runner.
4. Report: ready-to-sync (or what's blocking).

## B — Restart (operator, after close, flat-gated)

5. Confirm the market is closed and today's session/capture is done.
6. Run `bash "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/restart.sh"` — its built-in flat-account proof refuses if not flat; do not override.
7. Keep the Terminal window open (foreground server).

## C — Post-restart verification (Codex confirms; all must pass)

8. **Runtime:** `collector_version = entry-bid-collector-v1.2`, `PRELOCK_DRY_RUN`, cohorts unlocked, execution/Guard authority false.
9. **Regime v0.1 retained:** new episodes still carry FRESH regime tags mid-session.
10. **Runner measurement live:** confirm the measurement wrapper is attached on the Guard path (status/guard snapshot marker), so any runner-eligible position will write to `regime_flow_runner_shadow_v0.jsonl`. Confirm runner observations use `regime_layer_v0.classify_regime` over the collector's causal cached inputs, never the slow legacy `regime_model` path.
11. **Dense-source live:** re-labeled A2 endpoints carry `endpoint_source = alpaca_historical_stock_quote_v1` (not `live_tick_log`), and endpoint availability rises vs the gappy tick_log.
12. **A2 accrual metric present:** the audit reports `clean_a2_labelable_episode_count` separately from the option-bid scoreboard.
13. **Integrity:** next session's `session_date` correct; `unquarantined_collision_groups = 0`.

## D — Only then: the runner

14. Keep the runner default OFF. After step 10 is confirmed, a controlled per-position PAPER opt-in may exercise one real activation. The completed position must produce a `regime_flow_runner_shadow_v0.jsonl` row with the activation event and paired delta. If it does not, disable the opt-in and investigate. The default may change only after that row is verified and the operator deliberately changes it.

## Abort / rollback

- Regime shows `UNAVAILABLE` or A2 labels lack `endpoint_source` after restart → the new code didn't load (stale process/window); stop, verify no old instance survived, relaunch.
- `restart.sh` flat-account proof fails → a position is open; resolve it first, then restart.

## Notes

- This restart is also the natural moment the Cloud SQL fail-closed-staleness fix and any other pending disk code go live; confirm the mirror reports freshness fields afterward.
- Nothing here locks a cohort or changes a threshold. Option-bid NO-GO is unaffected and does not gate the A2 accrual.
