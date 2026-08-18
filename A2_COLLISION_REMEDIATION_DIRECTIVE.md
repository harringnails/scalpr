# A2 Cross-Side Collision — Remediation Directive

**Status: remediation directive. Paper/shadow, research-only.** No live trading, no Guard/order path, no detector/threshold/gate change, no fire-rate change, no exclude-and-rerun. Follows the M1 classification in the diagnostic report.

## Finding (confirmed)

All 14 cross-side co-timed collisions are **M1**: the legacy `has_reference → admit_episode` path admitted episodes without the `setup.qualified` check. **Zero M2** (no genuine both-sides-qualified pair) and **zero M3** (all timestamps microsecond-identical). The detector is not contradicting itself; the collision test is accurate. Mechanism is benign and understood.

**Two real blockers remain:**

1. **The fix is not running.** Current source (v1.2) is correct — admission requires `FRESH && qualified && reference_extreme_bucket` (`entry_bid_collector_v1.py:459`). But the live runtime is still **v1.1**: the one post-cutoff pair (group 14) reports `collector_version=entry-bid-collector-v1.1` and carries a stale `session_date=2026-08-11` on an Aug-13 decision. Until v1.2 is verified running, the prospective stream is **not** validated clean and cannot be trusted for accrual.
2. **Cross-side mutual exclusivity is not a code invariant.** `_evaluate_minute` loops CALL and PUT independently (`entry_bid_collector_v1.py:442`); overlap checks are same-side only (`entry_episode_research_v1.py:32`). No M2 was observed and it is near-impossible on the RSI gate, but it is not *enforced*. For an unattended year-long accrual, it must fail loud if it ever occurs.

## Operator actions (Chad — Mac, Keychain, localhost; Codex cannot do these)

Do these in order. **Do not restart while holdings != 0.**

1. **Confirm the paper account is flat** (`_require_flat_paper_account` / holdings == 0). If not flat, resolve the position first; do not restart on a live holding.
2. **Investigate the stale runtime before killing it.** The v1.1 process with a mismatched `session_date` may be a zombie or running on stale state. Note the PID and what state DB it's bound to, so the quarantine boundary (below) is accurate.
3. **Stop the stale v1.1 collector; deploy/restart on v1.2.**
4. **Verify the runtime version:** the collector status must report `collector_version=entry-bid-collector-v1.2` (not v1.1) before you trust any new admission.
5. **Run a clean post-restart collision scan** (`cross_side_timestamp_collisions` over post-restart admissions). Expect **0** cross-side groups. Report the count.

## Codex actions (research shell; no restart, no live path)

1. **Quarantine, do not delete (data-state honesty, §K).** Tag every admission through the last v1.1 write as `QUARANTINED_PREFIX_ADMISSION` in a manifest — including the 14 collision records and any other pre-v1.2 admissions. They are ineligible for inference and must never silently re-enter a set. Preserve them; mark them; exclude them explicitly with the reason recorded.
2. **Add the cross-side double-qualification integrity guard.** In the admission path, if both CALL and PUT pass `has_qualified_reversal` in the same minute, raise a loud `INTEGRITY_CROSS_SIDE_DOUBLE_QUALIFICATION` event and quarantine **both** records for manual review. Do **not** silently pick a side, do **not** drop them silently, and do **not** touch the detector, thresholds, or fire rate. This is a safety net for the year-long accrual, not a detection change. Ship it behind a test; it becomes the running version only after operator review.
3. **Fix the test environment.** Install `pytest` in the active project `.venv` and actually run: the qualification-gate admission test, the missing-endpoint→unavailable test, the non-overlap dedup test, and a new cross-side double-qualification guard test. Report pass/fail. No "suite passes" claim until the tests execute.
4. **Confirm the post-restart clean scan** once Chad reports v1.2 is running (Codex re-runs `cross_side_timestamp_collisions` over the post-restart admissions and confirms 0).

## Do NOT

- Do not rescue the 3 genuinely-qualified CALLs (groups 5, 8, 11) into any inferential set. They are pre-fix admissions **and** historical (unproven-provenance period) — ineligible on both counts. They only confirm the qualified path fires ~sparsely (~3 across ~6 sessions).
- Do not exclude collisions and re-run as a "fix." The remediation is: run v1.2, quarantine the pre-fix data, enforce the invariant.
- Do not change the detector, thresholds, gates, or fire rate.

## Gate status after remediation

- Prospective stream is trusted for accrual **only** once: (a) runtime verified `v1.2`, (b) post-restart collision scan returns 0, (c) the guard + tests pass under operator review.
- Phase-4 MVP box stays **open**. No edge claim under any outcome. The inferential verdict still comes only from clean prospective data the thresholds never saw.
- Reminder of the real bottleneck: even fully clean, the qualified fire rate (~3/6 sessions) puts the 200-episode gate ~1 year out. Raising it is the separate, gated Reversal v2 track (a new cohort, not a re-tune).
