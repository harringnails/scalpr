# FlashAlpha Pin Iron Condor Shadow Study v0 Pre-registration

Status: FROZEN before P&L implementation or outcome inspection.

This is an isolated, read-only, exploratory, non-inferential paper simulation.
It has no execution, order, admission, collector, server, Guard, A2, dense-store,
or prospective-cohort authority. Every output must retain
`execution_authority: false`.

The machine-readable frozen specification is
`flashalpha_pin_ic_prereg_v0.json`. Any rule change requires a new schema version,
new output paths, and a new cohort; v0 records must never be rewritten.

Frozen SHA-256:
`10f784b3ecd8ffb8eea0f594c67c8ac88d1aa418ae70e316bc1d6a090609edc7`.

## Fixed Design

- Entry is the first scanner observation from 14:00:00 through 14:05:59 ET.
- `HIGH_PIN_PRESSURE` at that observation is treatment. Every other priceable
  grade is the fixed-clock non-HIGH control.
- Sell one SPY 0DTE call at the data-derived call wall and one put at the
  data-derived put wall, using the nearest listed strikes with deterministic
  tie-breaking.
- Buy exact two-point wings. A missing exact wing makes the day unavailable.
- Use four contemporaneous FlashAlpha bid/ask mids. Quotes must be positive,
  non-crossed, no more than 60 seconds old, and no later than the response time.
- Haircut the net entry credit by $0.05 per leg ($0.20 per spread) and charge
  $0.65 per leg ($2.60 total). One spread uses the standard 100 multiplier.
- Hold to expiry with no stops, exits, rolls, or adjustments.
- Settle against the 15:59 ET SPY one-minute SIP bar close from Alpaca.
- Missing walls, contracts, prices, timestamps, or close data remain
  `UNAVAILABLE`; no quote, bar, theoretical-price, or strike substitution is
  permitted.

The primary comparison is net P&L on HIGH days versus priceable non-HIGH days
under the identical fixed-clock construction. The report emphasizes minimum,
5th and 10th percentiles, bottom-decile mean, worst sessions, and loss rate. A
contiguous five-session-block sign-flip null is descriptive only. No sample size
or p-value converts this study into an execution verdict.

SPY options are physically settled. The expiry payoff calculation is a paper
mark based on the underlying close and does not model after-hours assignment,
exercise exceptions, or operational handling around strikes. Those residual
risks make the simulation optimistic in that dimension despite conservative
entry costs.
