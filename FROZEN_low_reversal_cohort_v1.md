# Pre-Registration Draft — `low_reversal_v1`

**Status: DRAFT — NOT LOCKED. Paper/shadow only. No target session.**

The previous `820c1694…` value reproduced the old draft’s contents but did not establish when it was locked, did not contain operator confirmation, and referenced proxy labels. It is **not** the prospective cohort lock and must not be used as one.

## Hypothesis

After a mechanically defined intraday downside extension and confirmed reversal, a point-in-time eligible ~0.45-delta SPY call may have positive forward executable-bid expectancy from a simulated ask entry. This is an unvalidated hypothesis, not a trade recommendation or probability.

## Prospective-only boundary

- The 13 historical pilot observations are excluded: they predate this lock and use `delta_gamma_proxy`, not realized option bids.
- UW and IVolatility are `UNAVAILABLE`, not neutral, for this technical/execution baseline.
- Collection cannot begin until the forward bid observer is running and its coverage has been verified.
- Only first, non-overlapping reversal episodes count.

## Proposed mechanics

The complete machine-readable proposal is `frozen_cohort_low_reversal_v1.json`. In summary:

- CALL only; SPY options; 0–2 DTE
- Four required direction groups: 1.5× five-minute ATR extension, level proximity, momentum slowing, and completed-bar confirmation
- Contract target delta 0.45; proposed spread, volume, OI, price, and freshness gates
- Entry at fresh ask; all outcome observations on fresh option bids
- Proposed 20% native option risk and 1.5R target
- One 60-minute non-overlapping episode per reversal reference
- Cost-adjusted primary result remains unavailable until a realistic cost model is frozen

## Not yet approved

The JSON deliberately carries `operator_confirmed: false`, no target session, null implementation hashes, and an explicit `unconfirmed_fields` list. A draft hash is only a change-detection aid; it is not a cohort lock.

## Lock gate

Before a future open:

1. Review and confirm every value in `unconfirmed_fields`.
2. Verify forward bid capture on a non-cohort dry run.
3. Stamp rules, capture, outcome, episode, and resolved-config hashes.
4. Set a future target session and operator confirmation.
5. Change status to `READY_TO_LOCK` and run the fail-closed lock registry.

Any later parameter change requires a new cohort id. This draft contributes zero eligible observations.
