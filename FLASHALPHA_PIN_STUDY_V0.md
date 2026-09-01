# FlashAlpha SPY Pin Candidate Study v0

Status: exploratory, non-inferential, read-only, advisory, observational.

## Isolation

`flashalpha_pin_scanner_v0.py` imports only the isolated FlashAlpha logger and
standard-library modules. It has no collector, server, Guard, gate, admission,
broker, order, A2, dense-store, or prospective-cohort wiring. Raw HTTP records
remain in ignored `flashalpha_shadow_v0.jsonl`; candidate and outcome records go
to ignored `flashalpha_pin_study_v0.jsonl`; reports go to ignored
`flashalpha_pin_report_v0.json`.

Every artifact is stamped `EXPLORATORY - NON-INFERENTIAL`,
`is_qualifying: false`, `admission_authority: false`, and
`execution_authority: false`.

## Point-in-time Structure

Each poll reads SPY `gex`, `levels`, and `zero_dte`. The scanner retains the
provider's call/put OI and GEX by strike and computes only deterministic geometry:

- provider walls on the correct side of spot, choosing the nearest when both
  full-chain and 0DTE walls are present;
- if no provider wall exists on the correct side, the observed strike with peak
  absolute call GEX above spot or put GEX below spot, explicitly provenance-tagged;
- spot-to-wall distances in basis points;
- pocket `[put_wall, call_wall]`, width, and normalized spot position;
- gamma flip and positive/negative gamma regime;
- 0DTE GEX share, pin score, magnet, max pain, and agreement;
- endpoint states, provider timestamps, ages, and freshness.

No dark-pool floor or other synthetic price level is created. Missing data stays
missing.

## Frozen Candidate Rule

The provider's documented pin-score bands are retained: below 40 is `LOW`, 40
through 69 is `MODERATE`, and 70 or greater is eligible for `HIGH`.

Hard gates:

1. Negative gamma emits `ANTI_PIN_NEGATIVE_GAMMA`, never a pin candidate.
2. `HIGH_PIN_PRESSURE` requires positive gamma, complete fresh evidence, a pin
   score of at least 70, and no more than two hours to close. Earlier qualifying
   observations are capped at `MODERATE_PIN_PRESSURE`.

The maximum provider-data age is five minutes, matching the polling interval.
Post-close observations can supply outcomes but cannot become candidates.

## Outcome and Base Rate

Polling does not inflate N. At close, one canonical candidate is selected per
session: the highest grade observed intraday, with the earliest occurrence used
for ties. A fresh zero-dte observation whose provider timestamp is at or after
16:00 ET supplies the close price. If it is absent, the outcome stays unavailable.

`close_near_magnet` uses half the median observed strike spacing as its tolerance.
The report compares HIGH-day hit rate with:

- the close-near-magnet rate across every available day;
- the close-near-magnet rate across non-HIGH days.

The primary descriptive metric is lift over the non-HIGH base rate. HIGH-day
binary outcomes minus that base rate enter a one-sided sign-flip null where
contiguous five-session blocks share one sign, with `(k+1)/(n+1)` correction.
This mirrors the A2 dependence pattern locally without importing A2 code. The
result remains non-inferential regardless of p-value or sample count.

The default accrual caption target is 20 available sessions. Reaching 20 means
only that a multi-week descriptive report is ready, not that the signal is valid.

## Operator Commands

One real Growth-tier exploratory scan uses exactly three read-only calls:

```zsh
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-flashalpha-pin-scanner"
"/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/.venv/bin/python" \
  flashalpha_pin_scanner_v0.py scan --tier GROWTH
```

Poll every five minutes for a caller-selected number of observations:

```zsh
"/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/.venv/bin/python" \
  flashalpha_pin_scanner_v0.py poll --tier GROWTH --polls 79 --interval-seconds 300
```

After a fresh at-or-after-close observation has been logged:

```zsh
"/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/.venv/bin/python" \
  flashalpha_pin_scanner_v0.py score-outcome --session-date YYYY-MM-DD

"/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/.venv/bin/python" \
  flashalpha_pin_scanner_v0.py report --target-days 20
```

Basic/Free restrictions are preserved in the raw ledger. Without fresh
`zero_dte` evidence, the grade is `UNKNOWN`; no approximation is fabricated.
Declared Basic runs request only `gex` and `levels`; declared Free runs make no
SPY HTTP calls. Unavailable endpoints receive local `TIER_SKIPPED` records.
`--budget` can reduce the default maximum of three HTTP requests per poll;
budget-suppressed endpoints receive `CALL_BUDGET_SKIPPED` records.
