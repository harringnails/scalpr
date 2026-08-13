# Entry Intelligence v1 — Corrected Build Specification

**Status: implemented research foundation; not cohort-ready, not locked. Paper/shadow only.** No module in this subsystem imports broker/order code, affects Guard state, or has execution authority.

## Correction of the prior specification

`feature_engine.label_contract` is a `delta_gamma_proxy` label that starts from midpoint and explicitly excludes the realized option bid/ask path and fills. It is useful historical pilot evidence but **is not an executable-bid label and is not eligible for Entry Intelligence v1 outcomes**.

Entry Intelligence therefore uses a new, forward-only label contract:

- `entry-intelligence-option-bid-v1` records timestamped option bid/ask observations, including unavailable and stale observations.
- `entry-intelligence-executable-bid-outcome-v1` assumes entry at the fresh executable ask and measures only fresh executable bids afterward.
- Missing/stale bids are never imputed or carried forward.
- The old 13 proxy-labeled candidates are excluded from the prospective cohort.

## Three separate questions

Every axis is an `EvidenceAxis` containing an optional value, availability state, version, passed/failed inputs, and unavailable inputs. `UNAVAILABLE` and `STALE` axes must have `value: null`.

1. **Direction:** did the fully mechanical reversal conditions occur?
2. **Quality:** how much preregistered confluence is present?
3. **Executability:** is an eligible point-in-time option contract actually tradeable at a fresh two-sided quote?

There is **no aggregate field and no overall confidence** in v1. Every score is ordinal evidence and carries `is_calibrated_probability: false`.

## Mechanical reversal definition

Both sides use completed regular-session five-minute bars only. The current forming bar is excluded.

- ATR: simple mean True Range over 14 completed five-minute bars (`intraday-atr-v0` formula).
- Extension: decision close is at least 1.5 ATR from the opposite extreme of the preceding 12 completed five-minute bars.
- Level proximity: decision close is within 0.15% of prior-day extreme, premarket extreme, or the nearest whole dollar on the reversal side.
- Momentum slowing: the last two completed directional candles contract in high-low range, **or** Wilder RSI14 turns away from below 35 for CALL / above 65 for PUT.
- Confirmation: an exact three-bar higher-low/lower-high construction, **or** a completed-close reclaim/rejection of a registered level or causal session VWAP.
- All four groups are required. Any unavailable prerequisite yields `NO_TRADE`, never a neutral score.

The complete strings and values are in the two draft JSON files. They are proposals pending operator confirmation, not frozen trading parameters.

## Candidate and episode construction

The system may evaluate each RTH minute for observability, but only the first qualifying decision for a reversal reference can enter a cohort:

- Episode identity includes cohort, symbol, side, session date, reference-extreme five-minute bucket, and reference level.
- A duplicate reference is rejected.
- No candidate is admitted while the prior same-symbol/same-side 60-minute outcome window or cooldown remains open.
- Rejections are append-only evidence and do not count toward the 200-candidate gate.

This prevents repeated minute evaluations of one reversal from inflating sample size.

## Contract selection

Initial scope remains SPY options, 0–2 DTE. A contract must have:

- Fresh OPRA-derived bid/ask, currently proposed at age ≤5 seconds
- Spread `(ask-bid)/mid` ≤8%
- Volume ≥200 and open interest ≥500
- Absolute delta 0.40–0.50, target 0.45
- Ask ≤$2.50 per option share, equivalent to $250 per contract before fees

Among eligible contracts: nearest delta → lowest spread → highest OI → lexical option symbol. These thresholds remain explicitly unconfirmed.

## Native option-bid outcome geometry

The draft proposal uses entry at the executable ask and an option-native risk amount equal to 20% of entry ask:

```text
stop_bid   = entry_ask × (1 − 0.20)
target_bid = entry_ask + 1.5 × (entry_ask − stop_bid)
```

Only timestamped fresh executable bids can hit either level. The observer is proposed to sample every five seconds, require no gap over 15 seconds for horizon marks, and require 80% coverage for a completed no-hit label. At one provider timestamp, stop is resolved before target. Entry ask already captures the quoted spread; realistic slippage and fees are not calibrated, so net results remain `null` and exploratory.

## Decision-packet rules

`entry-intelligence-decision-v1` records the three axes, causal evidence, versions/hashes, episode identity, contract, and raw references.

- `CALL`/`PUT` requires selected contract plus frozen native option target and invalidation.
- `NO_TRADE` permits those fields to be null and must contain a missing/stale/failed reason.
- All evidence timestamps must be ≤ `decided_at`.
- Packets and lifecycle outcomes are append-only.

## Research test

The old regime-bar function is not reused directly. `entry-episode-session-block-sign-null-v1` operates on non-overlapping labeled episodes and their incremental return versus the preregistered baseline. Contiguous session blocks receive a shared random sign, preserving within-session/short-regime dependence; the one-sided p-value uses the finite-sample `+1` correction.

No “best” or edge claim is allowed until all are true:

- ≥200 admitted, non-overlapping, labeled candidates
- Realistic frozen cost model
- Untouched walk-forward window
- Primary metric clears the controlling session-block null
- No parameter changes after the prospective pre-open lock

## Lock procedure

The draft JSON files cannot be locked while they contain unconfirmed fields, missing implementation hashes, no target session, or `operator_confirmed: false`. A valid lock must be created before 09:30 America/New_York and appended to `entry_intelligence_cohort_locks_v1.jsonl`. A cohort id can never be relocked to different content.

## Files

- `entry_intelligence_v1.py`: contracts, mechanical reversal evidence, deterministic contract selection and episode identity
- `entry_bid_capture_v1.py`: forward bid records and executable-bid lifecycle outcomes
- `entry_episode_research_v1.py`: episode admission and session-block null
- `entry_cohort_lock_v1.py`: fail-closed pre-open lock registry
- `frozen_cohort_low_reversal_v1.json`: CALL-side draft
- `frozen_cohort_high_reversal_v1.json`: symmetric PUT-side draft

## Remaining activation gate

The code is not wired into the running server. Do not enable collection until the thresholds are operator-approved, implementation hashes are stamped, offline tests pass, and an explicit restart/activation is authorized. The first eligible cohort session must occur only after the forward bid logger is running and verified.
