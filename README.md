# Scalpr Trade — Project Brief

## What it is
A paper-trading platform whose core rule is a ratcheting profit ladder: as a trade climbs, the allowed giveback from its peak tightens, so gains get locked in progressively. Peaks only ratchet up, never down.

## Where it lives
`/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7` on a Mac (Python 3.14).

Three files:
- `scalp_server.py` — FastAPI backend + guard engine
- `dashboard.html` — UI it serves
- `precheck.py` — pre-trade signal module

Connects to Alpaca paper trading, free IEX feed.

### Start it
In Terminal:
```
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
python3 scalp_server.py
```
Then browser to `localhost:8420`.

## Features built
- Real-time quote feed with sparklines and freshness dots
- Ratcheting ladder with per-trade rungs
- 60-second grace period and 2-tick exit confirmation to prevent premature exits
- Stall timer
- Options contract builder with dropdowns that auto-default to the at-the-money strike and show live Bid/Ask/Last plus estimated cost
- Holdings panel with one-click liquidation and guard status
- Performance panel with win rate, P&L, profit factor from a CSV journal
- Pre-trade evidence check (8 signals from ETF proxies — trend, momentum, volatility, VIXY, SPY-vs-RSP breadth, SMH-vs-QQQ, TLT rates, HYG credit), presented as an honest uncalibrated checklist with no fake probability
- Market-closed and bad-symbol guardrails

## Operational gotchas learned the hard way
- Run the server in one Terminal tab and all commands in a second tab (Cmd+T), or commands queue instead of running.
- Browser caching hides updates constantly — use incognito or a no-cache header.
- Don't download new versions into new folders; it created seven of them.
- Deploy changes via in-place Python patch scripts pasted into Terminal, verified with a print statement.
- `lsof -ti:8420 | xargs kill` clears a stuck port.

## Trading lesson learned
Only trade near-the-money option strikes. Deep out-of-the-money contracts have no bid, can't be sold at any price, produce garbage percentages, and reject every liquidation attempt.

## Current status
Paper trading only, working, needs a real sample of trades to tune the ladder.

## Not yet built
- Always-on hosting for overnight holds
- DTE-split stats
- Tax estimator
- A larger market-regime platform (PRD exists)
