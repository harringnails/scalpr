# A2 Cross-Side Collision — Diagnostic Directive (for Codex)

**Status: diagnostic directive. Paper/shadow, research-only.** No live trading, no Guard/order path, no edits to any frozen cohort/spec, no detector/threshold/gate change. This is a **read-and-classify** task, not a fix. Governed by the frozen specs; do not alter them.

## Why this exists

The exploratory historical A2 dry run returned `FAIL_CROSS_SIDE_TIMESTAMP_COLLISIONS`: **14 collision groups across 28 episodes** — essentially every episode is a co-timed CALL/PUT pair. This is systematic, not sporadic, and it is **not** confined to the legacy set. Because a *qualified* bullish reversal requires RSI ≤ 35 and a *qualified* bearish reversal requires RSI ≥ 65, both sides being genuinely qualified at the same instant should be near-impossible. So the cause is a mechanism, and we must know which one **before** any re-run and before trusting prospective accrual.

**Do not "fix" this by excluding collisions and re-running.** Exclusion would hide the mechanism. Classify first.

## Task 1 — Classify the 14 collision groups (per-group evidence dump)

For **each** of the 14 co-timed groups, emit a record with, for both sides:

- `side`, `episode_key`, `decided_at`, and the **finest-grained** timestamp available (sub-minute if it exists) for each record
- `setup.status`, `setup.qualified`, `reference_extreme_bucket`
- the RSI value used, and the extension/level-proximity fields that drove the direction decision
- `admitted` and the admission reason/path

Then classify the group as one of:

- **M1 — historical generator omits the `setup.qualified` gate.** One or both records were admitted without `setup.qualified == True` (i.e., the historical reconstruction emits a record per side at each detected minute, pre-qualification). → Historical-tooling bug only; the fixed live collector is unaffected. *(Leading hypothesis, given ~100% pairing.)*
- **M2 — detector genuinely emits both directions at the same instant, both qualified.** Both records show `qualified == True` with contradictory RSI at the same instant. → Real design contradiction that also corrupts the prospective stream. Escalate.
- **M3 — timestamp-granularity false collision.** `decided_at` is minute-truncated, and the two records' finest-grained timestamps actually differ (two distinct reversals in the same minute, not the same instant). → Measurement/keying granularity issue, not data corruption.

Report the count of groups in each class. A group can only be one class; state the deciding field.

## Task 2 — Does the LIVE collector produce cross-side co-timed pairs? (the load-bearing check)

The prospective stream is the **only** basis for the eventual inferential verdict, so we must know it is clean.

- Scan the **post-fix** prospective episodes (those admitted after the `setup.qualified` admission fix) for any cross-side co-timed pairs, using the same `cross_side_timestamp_collisions` logic.
- Report: number of post-fix prospective episodes, number of cross-side co-timed groups among them, and — if any — the same per-group evidence dump as Task 1.
- Confirm in code whether `_evaluate_minute` can admit **both** sides in the same minute when both pass `has_qualified_reversal`, or whether qualification is in practice mutually exclusive per minute. State which, with the code path.

## Task 3 — Report, do not remediate

Return to the operator, before any re-run or fix:

1. The M1/M2/M3 classification counts + per-group deciding fields (Task 1).
2. Whether the live post-fix collector emits any cross-side co-timed pairs (Task 2), with the code-path determination.
3. A one-line recommended fix **scoped to the identified mechanism** — but do not implement it yet.

## Decision map (for the operator, once Codex reports)

- **All/most M1** → historical-tooling bug; live prospective stream is clean → prospective accrual continues safely; fix the historical generator's gating before any exploratory re-run.
- **Any M2** → design contradiction affecting live data → **pause reliance on the prospective stream**; fix the detector's direction commitment before accruing another episode.
- **M3 present** → tighten episode-key / collision granularity to sub-minute so genuinely distinct reversals aren't falsely merged; re-assess.

## Discipline

- Read-and-classify only. No detector/threshold/gate change, no cohort reuse, no `≤5s` freshness loosening, no exclude-and-rerun.
- The Phase-4 MVP box stays open throughout. No edge claim under any outcome.
