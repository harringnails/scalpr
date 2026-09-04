# Scalpr Option Scenario Engine v0

The option scenario engine is a localhost-only, read-only decision-support service. It performs deterministic arithmetic for the staged option when the operator runs pre-check. It cannot submit an order, alter the order payload, gate a trade, or write to any study or evidence ledger.

The dashboard labels every result `mechanical scenarios given an assumed move — not a forecast or a trade signal.`

## Inputs and arithmetic

- Contract definition: OCC symbol staged in the ticket.
- Spot: fresh Alpaca SIP two-sided quote midpoint, maximum age 60 seconds.
- Option premium: fresh Alpaca OPRA two-sided quote, maximum age 60 seconds. Breakeven uses the executable ask for a conservative long-entry basis; call breakeven is `strike + ask`, put breakeven is `strike - ask`.
- IV priority: fresh FlashAlpha `/optionquote/{underlying}` strike IV, maximum age 300 seconds; otherwise Black-Scholes inversion of the live OPRA midpoint. The model uses rate `0`, no dividends, ACT/365 time to 16:00 America/New_York on expiry day.
- Expected move: `spot * IV * sqrt(time-to-expiry-years)`, displayed as a symmetric range. This is an IV-implied range, not a directional forecast.
- Greeks priority: fresh FlashAlpha strike delta and gamma; otherwise Black-Scholes greeks derived from the inverted live midpoint IV.
- Scenario grid: SPY moves of `-2.0`, `-1.0`, `-0.5`, `+0.5`, `+1.0`, and `+2.0` points. Option change is `delta * move + 0.5 * gamma * move^2`; dollars multiply by contract count and 100.

Missing, stale, crossed, or unpriceable inputs remain unavailable. Breakeven still renders when a clean quote exists even if IV or greeks do not. FlashAlpha failure does not suppress the live-quote breakeven or the Black-Scholes fallback.

## Operator launch

Start the read-only scenario service before using pre-check:

```bash
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
./launch_option_scenario_panel_v0.sh
```

It listens on `127.0.0.1:8422`, accepts only `GET /health` and `GET /scenario`, and returns HTTP 405 for write methods. It uses Alpaca data clients only and reads the FlashAlpha API key from Keychain service `scalpr.flashalpha.api`.
