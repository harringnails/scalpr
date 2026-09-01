# FlashAlpha Pin Iron Condor Shadow Study v0

Status: exploratory, non-inferential, read-only paper simulation.

## Boundary

`flashalpha_pin_ic_shadow_v0.py` imports only the isolated FlashAlpha pin scanner
and logger. It has no broker trading client, collector, server, Guard, gate,
admission, order, A2, dense-store, or prospective-cohort wiring. It never places
or prepares an order. Every quote, entry, settlement, and report is stamped
`execution_authority: false` and written to ignored isolated paths:

- `flashalpha_pin_ic_quotes_v0.jsonl`: raw FlashAlpha chain and selected Alpaca
  close provenance;
- `flashalpha_pin_ic_study_v0.jsonl`: one immutable entry and settlement per
  session;
- `flashalpha_pin_ic_report_v0.json`: descriptive P&L, controls, and loss tail.

The frozen machine-readable design is `flashalpha_pin_ic_prereg_v0.json`, SHA-256
`5a66278075eb1eeacafbafcb59c7971b7ea882c6b79edf5746a36f76550c19a8`.

## Pricing Semantics

FlashAlpha documents the Growth-tier live endpoint
`https://lab.flashalpha.com/optionquote/SPY`. Filtering by the same-session
expiry returns contracts with bid, ask, mid, and `lastUpdate`. The simulator
recomputes each mid from clean bid/ask rather than trusting or fabricating a
price. All four legs must be present at exact listed strikes, positive and
non-crossed, no more than 60 seconds old, and contemporaneous with the frozen
entry candidate.

FlashAlpha historical point-in-time option chains are Alpha-tier. Growth can
therefore collect prospective entries but cannot retroactively price an old pin
candidate. Existing days without retained entry quotes remain `UNAVAILABLE`.

The underlying settlement mark is the exact 15:59 ET close from Alpaca's SPY SIP
one-minute bar. A missing SIP bar is not replaced with FlashAlpha spot, IEX,
another minute, a daily bar, or a theoretical value.

## Frozen Simulation

At the first scanner observation from 14:00:00 through 14:05:59 ET, the study
sells the listed call nearest the call wall and the listed put nearest the put
wall, then buys exact two-point wings. A HIGH grade is treatment; every other
priceable grade is the same-clock control. It holds one spread to expiry with no
management.

The four-leg mid credit receives a $0.05-per-leg adverse-fill haircut and four
$0.65 commissions. Settlement subtracts the bounded put and call spread
intrinsic values from that adjusted credit. The report exposes the worst five
sessions, minimum, 5th percentile, 10th percentile, bottom-decile mean, loss
rate, and net expectancy after costs.

The primary comparison is HIGH-day mean net P&L minus non-HIGH fixed-clock mean
net P&L. A one-sided sign-flip null applies one sign to each contiguous
five-session block with finite-sample plus-one correction. It remains a
descriptive smell test regardless of sample count or p-value.

## Operator Commands

Run the combined scanner and study poll shortly after 09:30 ET. It makes the
scanner's three calls per observation plus one option-chain call in the fixed
entry window:

```zsh
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-flashalpha-pin-scanner"

caffeinate -i \
  "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/.venv/bin/python" \
  flashalpha_pin_ic_shadow_v0.py study-poll \
  --polls 79 \
  --interval-seconds 300
```

After Alpaca has published the completed 15:59 ET SIP bar:

```zsh
"/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/.venv/bin/python" \
  flashalpha_pin_ic_shadow_v0.py settle \
  --session-date YYYY-MM-DD

"/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/.venv/bin/python" \
  flashalpha_pin_ic_shadow_v0.py report \
  --target-days 60
```

The authenticated calls are operator-run and read credentials only from macOS
Keychain. Neither credential is written to an artifact or printed.

## Interpretation

A positive HIGH-day average is not enough. The signal must improve expectancy
over the non-HIGH fixed-clock control after costs while its failed-pin loss tail
remains tolerable across a materially large sample. Even then this v0 report is
exploratory and non-inferential; it cannot authorize trading.
