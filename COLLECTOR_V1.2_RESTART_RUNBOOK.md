# Collector v1.2 Restart Runbook (end-of-day, operator-run)

**Status:** operational runbook. Paper/shadow only. **Operator-run on the Mac** — Codex cannot do this (no Keychain, no localhost). No code change is required; this is purely an operational restart.

## The one fact that makes this simple

The source already reads `COLLECTOR_VERSION = "entry-bid-collector-v1.2"` (`entry_bid_collector_v1.py:28`). The live runtime reports **v1.1** only because the running process was started before the bump and is stale. **A clean restart makes the runtime load v1.2.** The version string flipping to v1.2 is your primary success check.

## When to run

- **After the RTH close (~16:00 ET), once today's study capture has wound down.** Do **not** restart mid-session — it can interrupt legitimate market-hours capture and risks tripping the flat-account guard on an open paper position.
- Restarting while the market is closed loses **zero** capture (the collector sits in `ARMED_MARKET_CLOSED`).
- Today's v1.1 capture does **not** count toward clean accrual regardless; the clean clock starts at the restart.

## Pre-checks (all must be true before you touch the process)

1. Market is closed and the day's study/capture is done.
2. **Paper account is flat** (holdings == 0 / `_require_flat_paper_account` would pass). If any position is open, resolve it first — **never restart on an open position.**
3. On-disk code is the reviewed v1.2: clean git working tree at the reviewed commit, and `.venv` tests green (`pytest` → currently 52 passed).

## Restart procedure (operator Terminal)

> Commands marked ⟨…⟩ are environment-specific — substitute your actual launch/stop command. The safety and verification steps around them are exact.

4. **Identify what's running.** Find the collector/server process and note its PID:
   ```
   ps aux | grep -Ei 'scalp_server|entry_bid_collector' | grep -v grep
   ```
   Check for a watchdog/auto-restart wrapper (the presence of `scalpr_autorestart.log` implies one may exist). **If a supervisor is running, restart *through* it or stop it first** — otherwise it will respawn the old process and you'll fight the restart.

5. **Record the quarantine boundary.** Note the last-written admission timestamp in the episode ledger — this is the final v1.1 admission; everything at/before it stays quarantined:
   ```
   tail -n 1 entry_intelligence_episodes_v1.jsonl
   ```

6. **Load credentials** (Keychain-backed; do not echo keys):
   ```
   source load_keychain_env.sh
   ```

7. **Stop the process gracefully.** Avoid `kill -9` mid-write (it can leave a torn JSONL/DB line). Send a normal termination, confirm the process — **and any watchdog** — is gone, and confirm **no second/zombie instance** remains (the Aug-11-session-date-on-an-Aug-13-decision symptom Codex saw was a stale runtime):
   ```
   ps aux | grep -Ei 'scalp_server|entry_bid_collector' | grep -v grep   # expect no rows
   ```

8. **Start on v1.2** using your normal launch command ⟨…⟩. Confirm it boots: flat-account guard passes, universe loads, a fresh status is written.

## Verification gate (every item must pass before you trust the stream)

9. **Runtime version = v1.2.** The status output must report `"collector_version": "entry-bid-collector-v1.2"`. If it still says v1.1 → a stale process survived or the wrong entrypoint launched. Stop, kill all instances, verify, relaunch. **Do not proceed until this reads v1.2.**

10. **Quarantine boundary intact.** Re-run the prefix quarantine over any v1.1 admissions written today so none slipped past the existing manifest; confirm all pre-restart admissions are marked ineligible (`eligible_for_a2_measurement=false`, `…_phase4=false`, `…_cohort_reuse=false`).

11. **Post-restart collision scan = 0.** Run `cross_side_timestamp_collisions` over **only post-restart admissions**; require **0** groups. (The post-restart set is empty until the next RTH session produces admissions — confirm 0 trivially now, then confirm 0 again after the first clean session.)

12. **Integrity healthy.** Status is `ARMED_MARKET_CLOSED` now (→ `ACTIVE_RTH_CAPTURE` at next open), **not** stuck in `DEGRADED_FAIL_CLOSED`, and there are **no** `INTEGRITY_CROSS_SIDE_DOUBLE_QUALIFICATION` rows in `entry_intelligence_integrity_events_v1.jsonl`.

13. **Record the restart timestamp.** This is t0 of the clean prospective accrual clock. Everything before it is v1.1 and excluded.

## Abort / rollback conditions

- Runtime still reports v1.1 → stale process survived or wrong module launched → kill all instances, verify none remain, relaunch.
- Any post-restart cross-side collision → do **not** trust the stream; investigate before accruing.
- Flat-account guard fails on boot → an open position exists → resolve it, then restart.

## After the restart

- Hand back to Codex to confirm, on the next session's data: post-restart collision scan returns 0, and new episodes carry `regime_tag`.
- **Stage-1a will keep reading `INSUFFICIENT_TAGGING` for weeks** until enough clean episodes accrue. That is expected, not a fault.
- The clean prospective accrual clock is now running. At the v1 fire rate, the ≥200-episode gate is ~12–18 months out; that timeline is the open Reversal-v2 decision, not an engineering task.
