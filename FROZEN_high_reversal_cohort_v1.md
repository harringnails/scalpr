# Pre-Registration Draft — `high_reversal_v1`

**Status: DRAFT — NOT LOCKED. Paper/shadow only. No target session.**

This is the symmetric PUT-side companion to `low_reversal_v1`. It is intentionally drafted only after the shared mechanics were made explicit, so the two sides do not acquire separate researcher degrees of freedom.

## Hypothesis

After a mechanically defined intraday upside extension and confirmed rejection, a point-in-time eligible ~0.45-absolute-delta SPY put may have positive forward executable-bid expectancy from a simulated ask entry. This is an unvalidated hypothesis, not a trade recommendation or probability.

## Symmetry contract

The PUT cohort shares the low cohort’s ATR method, lookback, proximity, contract gates, option-native risk, quote freshness, outcome coverage, episode rule, cost limitation, sample gate, and null test. Only the directional mirror changes:

- Prior-day/premarket **high** instead of low
- Whole-dollar level at or above price instead of at or below
- Upside extension from the rolling low
- Wilder RSI14 turning down from above 65
- Lower-high confirmation or completed close back below resistance/VWAP
- PUT contract instead of CALL

The complete proposal is `frozen_cohort_high_reversal_v1.json`.

## Prospective-only and lock gate

There are no eligible historical observations. The file remains `operator_confirmed: false`, has no target session, and has null implementation hashes. It must pass the same dry-run, confirmation, hash-stamping, future-session, and append-only pre-open lock process as the low cohort. Until then it contributes zero eligible observations.
