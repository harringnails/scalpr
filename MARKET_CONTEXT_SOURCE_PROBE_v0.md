# Market Context Source Probe v0

**Probe date:** 2026-09-03
**Scope:** existing read-only Alpaca SIP stock client and existing FlashAlpha shadow records. No authenticated request was made for this probe.

| Context family | Status | Existing source | Point-in-time fields admitted to v0 |
|---|---|---|---|
| SPY structure | FEEDABLE | Alpaca SIP stock quotes + 1-minute bars | bid/ask midpoint, spread, session VWAP proxy, VWAP slope proxy, 30-minute opening range, realized-volatility proxy |
| ETF cross-asset | FEEDABLE | Alpaca SIP stock quotes + bars | QQQ, IWM and fixed sector-ETF session returns |
| Large-cap breadth | FEEDABLE AS PROXY | Alpaca SIP stock quotes + bars | fixed ten-name large-cap advancing fraction, always labeled `largecap_breadth_proxy`; never S&P 500 breadth |
| Options structure | FEEDABLE | Existing FlashAlpha pin-shadow ledger from `gex`, `levels`, and `zero_dte` | spot, gamma regime, gamma flip, call/put walls, 0DTE GEX share, wall distances, toward/through level context |
| VIX index | NOT CONFIRMED | None in the current Alpaca US-equity SIP client | excluded |
| ES futures | NOT AVAILABLE | Current Alpaca stock client has no futures feed | excluded |
| Treasury yields | NOT AVAILABLE | No point-in-time yield source in the current stack | excluded |
| Dollar index | NOT AVAILABLE | No confirmed index/futures source in the current stack | excluded |
| Full 500-name breadth | NOT AVAILABLE | Would require a separately maintained constituent universe and full streaming capture | excluded |
| Executed up/down volume | NOT AVAILABLE | Current quote capture contains displayed quote sizes, not executed trades | excluded |
| Real-time news/catalysts | NOT AVAILABLE | No timestamped news-wire source in the current stack | excluded |

FlashAlpha availability is based on stored provider responses already captured by the isolated Growth-tier scanner. Its cadence remains the scanner's cadence, not tick-level; the context sampler records its provider timestamp and degrades/stales it rather than pretending it is contemporaneous.

The v0 sampler therefore records only the first four rows. It has no execution, admission, order, collector, A2, dense-store, or Guard authority.
