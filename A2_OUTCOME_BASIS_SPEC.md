# A2 Outcome Basis Spec

**Status:** pre-registered design spec
**Scope:** Stage A2 for the reversal study
**Mode:** paper / shadow only

## Decision

The current single-option executable-bid outcome basis is not reliably viable as specified.

For v2, the outcome basis is therefore **underlying-forward-return**, not greeks-mapped option P&L.

For the edge test, the basis is always evaluated as a **direction-adjusted signed return** so bullish and bearish reversal setups are measured on the same scale.

## Why this basis

The executable-bid path failed on labelability because quote density is structurally bimodal across strikes.

The underlying-return basis avoids that failure mode:

- it does not depend on a single option strike staying fresh for the entire horizon
- it is labelable on the same underlying feed used for the setup
- it can be measured consistently across sessions
- it is less sensitive to 0DTE gamma/theta distortion than a greeks-mapped option-return estimate

## Basis definition

For each frozen, non-overlapping setup episode timestamp `t0`, compute underlying forward return at fixed horizons:

- `return_5m`
- `return_15m`
- `return_30m`
- `return_60m`

Primary label for the MVP question:

- `return_60m`

Auxiliary horizons:

- `return_5m`, `return_15m`, `return_30m`

The measurement should use a point-in-time clean underlying price source and preserve missingness explicitly.

### Signed-return convention

Reversal setups are directional:

- bullish reversal setup → sign `+1`
- bearish reversal setup → sign `-1`

The primary test metric uses:

- `signed_return_60m = return_60m × setup_direction_sign`

The same sign convention applies to the shorter auxiliary horizons.

Positive signed return means the underlying moved in the predicted direction. Negative signed return means it moved against the prediction.

## Recommended measurement convention

- Use the underlying SPY price at entry timestamp as the anchor.
- Use the underlying SPY price at each future horizon boundary as the endpoint.
- Keep the sampling rule fixed before any analysis.
- If the exact endpoint is missing, do not impute it silently.
- Count only the first observation per non-overlapping episode; repeated re-evaluations of the same setup window do not create new observations.

## Bias controls

The basis must be pre-registered with these controls:

- Fixed horizons only. No horizon sweeping.
- Fixed anchor price definition. No mid/last substitution games after the fact.
- Missing values stay missing.
- No peeking at future regime labels.
- Use the same session-block matched null and walk-forward discipline planned for the edge test.
- Preserve the non-overlap / episode-key discipline from `REVERSAL_V2_DESIGN_BRIEF.md`; correlated peeks at the same move do not count as new evidence.

## Primary statistic

The primary edge statistic is the **mean signed 60m return** across the frozen, non-overlapping reversal episodes.

The null comparison is the **session-block matched null** already reserved for the edge test.

Finite-sample p-values use the standard +1 correction:

- `p = (k + 1) / (n + 1)`

where `k` is the number of null draws at least as favorable as the observed statistic and `n` is the number of null draws.

## Success criterion

The edge test is positive only if all of the following are true:

- mean signed 60m return is positive
- one-sided matched-null p-value is `<= 0.05`
- walk-forward evaluation preserves the same sign on held-out folds

If any of those fail, the honest verdict is no edge.

## Measurement pipeline

Reuse the existing `entry_policy_prototype` outcome machinery where possible:

- `return_5m`
- `return_15m`
- `return_30m`
- `return_60m`
- `mfe`
- `mae`

That gives a labelable outcome stream independent of single-option quote density.

The `return_60m` horizon stays primary because it matches the existing hourly label window used for this study family and avoids post-hoc horizon shopping; the shorter horizons remain auxiliary diagnostics for decay, not primary alternatives to be selected after the fact.

## What this basis is not

- Not a claim of predictive edge
- Not a live-trading signal
- Not a replacement for all option-specific analysis
- Not a greeks-optimized synthetic option return
- Not a statement that a directional edge is directly tradeable as a profitable 0DTE options strategy after spread, theta, slippage, and execution costs

## Expected use

This basis is the gate for the cheap directional-edge test:

- frozen reversal setup
- underlying-forward-return labels
- session-block matched null
- walk-forward verification

If the reversal setup shows no edge here, the honest conclusion is to pivot away from more reversal variants.
