# Reversal Phase 4 Edge Test Spec

**Status:** pre-registered test spec
**Scope:** the cheap directional-edge test for the frozen reversal setup
**Mode:** paper / shadow only

## Purpose

This spec answers one question:

> Does the frozen reversal setup show directional edge on the A2 underlying-forward-return basis?

This is not a design spec for a new strategy. It is a test harness for the edge verdict.

## Prerequisites

- A2 basis frozen in [`A2_OUTCOME_BASIS_SPEC.md`](./A2_OUTCOME_BASIS_SPEC.md)
- Frozen reversal setup, no threshold changes, no cohort reuse
- Non-overlap / episode-key discipline from [`REVERSAL_V2_DESIGN_BRIEF.md`](./REVERSAL_V2_DESIGN_BRIEF.md)

## Validity Gate

The verdict is only valid if there are at least **200 non-overlapping labeled episodes**.

- If `N < 200`: verdict is **underpowered / inconclusive**
- If `N >= 200`: proceed to the edge verdict

This is a power gate, not a style preference. A null result below the floor does **not** mean no edge.

## Episode Definition

Use only the first observation for each non-overlapping reversal episode.

- One setup timestamp = one episode key
- Re-evaluations of the same setup window do not create new episodes
- Cooldown / overlap logic from the reversal cohort spec stays in force

## Walk-Forward Fold Scheme

The walk-forward check is fixed before analysis and uses **4 contiguous chronological folds**.

### Construction

- Sort qualifying episodes by setup timestamp.
- Partition them into 4 contiguous folds of nearly equal episode count:
  - Fold 1 = earliest quarter
  - Fold 2 = second quarter
  - Fold 3 = third quarter
  - Fold 4 = latest quarter

### Rules

- No refitting on later folds.
- No re-partitioning after looking.
- No horizon changes, no cohort changes, no threshold changes.
- Fold boundaries are fixed by chronology only.
- The walk-forward boundary is an **expanding-window, next-fold-held-out** scheme:
  - fold 1 is the earliest block
  - fold 2 is the first held-out block
  - fold 3 is the second held-out block
  - fold 4 is the final held-out block
- For each held-out fold `i`, any reference thresholding, null calibration, or summary chosen for the verdict must use only folds strictly before `i`.
- The held-out fold itself is never used to set the rule that judges it.

### Fold-level requirement

Each fold must have a positive mean **signed 60m return**.

If any fold is zero or negative, the walk-forward stability check fails.

## Primary Test Statistic

Primary statistic:

- mean signed 60m return across all qualifying non-overlapping episodes

The primary null comparison remains the session-block matched null already defined in the A2 basis spec.

Use the same finite-sample +1 correction:

- `p = (k + 1) / (n + 1)`

where `k` is the number of null draws at least as favorable as the observed statistic and `n` is the number of null draws.

## Verdict Rules

### EDGE

Return EDGE only if all of the following hold:

- `N >= 200`
- mean signed 60m return is positive
- one-sided matched-null `p <= 0.05`
- all 4 walk-forward folds have positive mean signed 60m return

### NO EDGE

Return NO EDGE only if:

- `N >= 200`, and
- the test fails the statistic or walk-forward stability

### UNDERPOWERED / INCONCLUSIVE

Return UNDERPOWERED / INCONCLUSIVE if:

- `N < 200`

## What This Test Is Not

- Not a live-trading authorization
- Not a 0DTE options profitability claim
- Not proof that a directional SPY edge is directly monetizable after spread, theta, slippage, and execution costs
- Not a license to loosen the setup or the basis after the fact

## Reporting Format

Report all three verdict components explicitly:

1. power gate result
2. matched-null statistic and p-value
3. walk-forward fold stability

The final human-readable verdict should be one of:

- EDGE
- NO EDGE
- UNDERPOWERED / INCONCLUSIVE
