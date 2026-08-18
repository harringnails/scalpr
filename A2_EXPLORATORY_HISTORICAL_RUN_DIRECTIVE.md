# A2 Exploratory Historical Run — Directive (for Codex)

**Status: build/run directive. Paper/shadow, research-only.** No live trading, no Guard/order path, no edits to any frozen cohort/spec. Governed by the frozen `A2_OUTCOME_BASIS_SPEC.md` and `REVERSAL_PHASE4_EDGE_TEST_SPEC.md` — **do not change either; run to them.** Resolves Part 0 of `A2_MEASUREMENT_BUILD_DIRECTIVE.md`.

## Part 0 determination (the hard gate, now resolved)

**Question:** were the v1 reversal thresholds (`1.5×` 5-min ATR extension, RSI14 `35/65`, `0.15%` level proximity, momentum-contraction and confirmation rules) chosen without tuning on the SPY historical period to be tested?

**Determination: UNPROVEN.** The operator cannot establish, from records or recollection, that these thresholds were set without exposure to SPY history. Provenance is also not provable from repository evidence (`A2_HISTORICAL_VALIDITY_DETERMINATION.md`), and the git history was flattened in the backup baseline, so no commit trail exists.

**Consequence (per the pre-registered gate):** the historical SPY period is treated as **potentially in-sample**. A historical reconstruction therefore **cannot** produce the inferential edge verdict. It may be run **only as exploratory characterization and pipeline validation**, under the constraints below. The inferential Phase-4 verdict comes **only** from prospective data the thresholds never saw.

This is the honest default: when you cannot prove a period is out-of-sample, you do not treat it as if it were.

## What to run (exploratory)

1. **Detector, byte-frozen.** Run the v1 reversal detector, unchanged, over historical SPY 5-min RTH bars (Alpaca historical stock bars, `data.alpaca.markets` — operator-authenticated pull from the Terminal, not an agent shell). Do not modify the detector, thresholds, or any gate to change the fire rate; that would be a new cohort.
2. **Label per A2 spec.** For each detected **non-overlapping** episode, produce an A2 label via the Part-1 labeler: `signed_return_60m` primary; `5/15/30m` auxiliary; anchor = point-in-time SPY price at `t0`; endpoints = SPY price at each horizon boundary; **missingness stays missing** (exclude, record the exclusion); one observation per episode key + cooldown.
3. **Exclude the legacy collisions.** The 13 co-timed CALL/PUT legacy pairs (the fixed unqualified-reference admission bug) are an integrity failure and stay excluded from every set.
4. **Run the full Phase-4 machinery as a dry run** — session-block matched null (`p = (k+1)/(n+1)`) and 4-fold contiguous chronological walk-forward — to exercise the harness end to end on real volume.

## How the output MUST be labeled (non-negotiable)

- Every artifact, record, and report stamped **`EXPLORATORY — NON-INFERENTIAL`**, `is_inferential: false`.
- **No `EDGE` / `NO EDGE` verdict is emitted.** Report only descriptive statistics: episode count (by side and period), mean `signed_return_60m`, the matched-null p-value, and per-fold signs — each explicitly flagged as **not** the MVP verdict.
- The **Phase-4 MVP box stays OPEN.** This run cannot close it.
- State the two purposes plainly in the report: (a) **pipeline validation** — the A2 labeler + null + walk-forward exercised on real volume; (b) **directional smell test** — a hint of whether the reversal points the right way, to inform where effort goes next.

## Guardrails / discipline

- No detector/threshold/gate change to alter fire rate. No cohort reuse. No `≤5s` freshness loosening.
- A **flat or negative** exploratory result is a useful redirect signal — it is **not** a "no edge" claim.
- A **positive-looking** exploratory result is **not** permission to claim edge or go live. It only raises **prospective confirmation** to priority.
- Labeled set is data (gitignored); code/specs committed.
- Report the episode count and these descriptive statistics back to the operator; no edge claim under any outcome.

## Parallel track — prospective accrual (the only inferential path)

- The now-fixed collector (admission requires `setup.qualified`) accrues fresh, clean episodes going forward with the frozen detector.
- These prospective episodes — never seen by the thresholds — are the **only** basis for the eventual inferential Phase-4 verdict.
- Timeline at the v1 fire rate (~4 distinct fires / 6 sessions) is ~1 year to the 200-episode gate; do not shortcut it. (Raising the fire rate is the separate, gated Reversal v2 track — a new cohort, not a re-tune.)
