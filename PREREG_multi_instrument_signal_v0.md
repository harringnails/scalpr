# Pre-Registration - Multi-Instrument Signal Study (v0)

**Study ID:** `multi_instrument_signal_v0` (D1 flip-reclaim; D2 intraday-continuation)  
**Status:** **FROZEN / operator-approved 2026-09-04.** Paper/shadow and `UNDERPOWERED` until the frozen verdict requirements are met. It has no admission, Guard, order, or execution authority.  
**Prospective accrual:** begins with the next full RTH session after the freeze commit. Every session on or before 2026-09-04 is in-sample and excluded.  
**Mutation rule:** any parameter, instrument, marker, outcome, null, or verdict change requires a new study ID and resets N to zero. Every record carries this file's SHA-256.

## Frozen Instrument Set

`SPY`, `QQQ`, `IWM`, `DIA`. These have American-style, physically settled ETF options. `SPX` and other European-style cash-settled indexes are excluded because their mechanics are not pooled with ETFs.

## Frozen Relative Thresholds

| Parameter | Frozen value |
|---|---:|
| D1 acceptance band `A` | 2.5 bps of that instrument's spot |
| D2 sweep depth `S` | 3.0 bps of that instrument's spot |
| Reclaim window `R` | 180 seconds |
| Acceptance window `W` | 900 seconds |
| Back-below grace `G` | 120 seconds cumulative |
| Proxy-VWAP slope window `V` | 2 minutes |
| D2 acceptance | at least 3 consecutive positive 1-minute quote-mid returns |

All point thresholds are computed from each instrument's own spot. Executed volume is excluded from D2. Proxy-VWAP is a time-series quote-mid proxy, never traded VWAP.

## D1 - Prior-Regime Flip-Reclaim

For each instrument, the final fresh D-1 FlashAlpha read must have `gamma_regime == negative` and `spot < gamma_flip`. Freeze `F = D-1 gamma_flip` at D open; intraday reads cannot change F. The first reclaim of F must hold at or above `F - A` for `W`, allowing no more than `G` cumulative seconds below F. `t0` is acceptance completion.

H1a is a premarket reclaim followed by RTH acceptance. H1b opens below F and crosses from below during RTH. They remain separate cohorts with one episode per instrument/session/cohort and separate pooled targets.

## D2 - Intraday Continuation

Markers must occur in order: a 3.0-bps downside sweep beyond a local extreme; reclaim within 180 seconds; at least three consecutive positive 1-minute quote-mid returns; proxy-VWAP reclaim with positive two-minute slope; spot crosses the nearest call wall; that wall migrates upward on the next five-minute FlashAlpha read. `t0` is that migration-confirmation provider timestamp. No outcome begins before confirmation. One episode per instrument/session.

## Inputs, Outcomes, and Missingness

FlashAlpha Growth full-chain GEX, levels, and 0DTE reads provide call wall, put wall, gamma flip, regime, and spot. Every endpoint and the aligned Alpaca SIP quote carries provider/request/response timestamps and age. A stale or missing instrument is excluded and logged without blocking another instrument.

Each episode uses its instrument's own Alpaca SIP two-sided, non-crossed midpoint at anchor and fixed 5/15/30/60-minute horizons. Each quote must be no more than 5 seconds from its target. All five points are required; otherwise the episode is `A2-UNAVAILABLE`, excluded, and never imputed or widened.

## Frozen Analysis and Verdict

- Primary pooled target: `N = 150` per D1 cohort (H1a and H1b separately) and `N = 150` for D2.
- Null controls: same instrument, session-time block, and realized-volatility bucket. The frozen estimator is `p = (k + 1) / (n + 1)`.
- Walk-forward: four chronological folds; at least 3/4 must have the same positive sign.
- Secondary reporting: count/effect/sign by instrument. `REPLICATED` means at least 3/4 instruments have the pooled effect's sign; it is reported, not a primary gate.
- `EDGE`: N met, pooled effect positive, p <= 0.01, and at least 3/4 fold signs positive.
- `NO EDGE`: N met but an EDGE condition fails.
- `UNDERPOWERED`: N below 150.

Guard operability remains a separate study and cannot enter this signal verdict. Historical data is exploratory/non-inferential and cannot count toward N.

## Rate Budget Frozen With Cadence

FlashAlpha Growth responses document 2,500 requests/day. The four-symbol five-minute poll uses at most 79 x 4 x 3 = 948 requests. Including the unchanged SPY pin poll (237) and one option-chain entry call gives a conservative 1,186 requests/day, leaving 1,314 calls (52.6%) headroom. HTTP 429 stops the multi-instrument poll and is logged; symbols are never silently dropped.
