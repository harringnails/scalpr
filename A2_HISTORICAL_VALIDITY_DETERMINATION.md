# A2 Historical Reconstruction Validity Determination

**Status: NOT CLEARED for an inferential historical Phase-4 run.**

## Question

Were the frozen v1 reversal direction thresholds selected a priori, or tuned on
historical SPY data?

The relevant rules are the 1.5x five-minute ATR extension, 35/65 Wilder RSI
turn, 0.15% level proximity, momentum-contraction, and causal-confirmation
rules.

## Repository Evidence

- [`APPROVED_THRESHOLDS_low_high_reversal_v1.md`](./APPROVED_THRESHOLDS_low_high_reversal_v1.md)
  records operator approval on 2026-08-06.
- [`frozen_cohort_low_reversal_v1.json`](./frozen_cohort_low_reversal_v1.json)
  and its mirrored PUT cohort freeze the exact rule strings and parameters.
- [`FROZEN_low_reversal_cohort_v1.md`](./FROZEN_low_reversal_cohort_v1.md)
  excludes the 13 earlier proxy-label observations from the prospective cohort.
- No checked-in document or source file records a parameter search, historical
  SPY optimization, or outcome-based selection of these thresholds.

## Determination

The repository supports that the v1 rules are fixed and that the earlier
proxy-label observations are excluded. It does **not** establish how the
thresholds were originally selected. Absence of a tuning record is not evidence
that the thresholds were a priori.

Therefore historical reconstruction may be used only for engineering and data
coverage checks until an operator attestation or contemporaneous design record
confirms that the thresholds were not selected or optimized on the proposed
historical period.

## Consequences

- The A2 labeler may run on the local archive for measurement validation.
- The local pre-fix episode ledger contains 26 admitted records in 13 exact
  CALL/PUT timestamp pairs. This happened because the collector admitted a
  reversal *reference* even when the mechanical setup was not qualified. Those
  pairs are not independent directional evidence and cannot count toward the
  200-episode gate. The collector predicate was corrected in
  `entry_bid_collector_v1.py`; the append-only legacy ledger is preserved but
  ineligible for Phase-4 inference.
- The current local archive is an underpowered engineering run, not an edge
  test.
- Do not fetch or use broader historical SPY data for the inferential Phase-4
  verdict until this gate is cleared.
- If the thresholds were tuned on any historical interval, use only an untouched
  held-out interval or prospective accrual for the inferential test.
