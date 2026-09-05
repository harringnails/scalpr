# Market Structure Candlestick Chart v0

## Scope

This chart is a read-only, display-only shadow surface. It has no execution,
admission, order, Guard, or study authority. Alpaca SIP trade OHLC bars are for
display and remain distinct from the frozen studies' quote-mid outcome basis.
Episode triangles show only already-accrued `t0` anchors and are not signals.

## Inputs

- Alpaca SIP 1-minute or native 5-minute trade bars for SPY, QQQ, IWM, and DIA.
- Latest point-in-time FlashAlpha structure from
  `multi_instrument_flashalpha_v0.jsonl`.
- Counted episode anchors from `multi_instrument_signal_v0.jsonl`,
  `prior_regime_flip_reclaim_v0.jsonl`, and
  `intraday_continuation_v0.jsonl`.

The bridge only accepts GET requests. It never opens a source ledger for write
or calls an account/order endpoint. ECharts 5.6.0 is vendored under `vendor/`
so the local dashboard has no CDN dependency.

## Freshness

- 1-minute candle: at most 180 seconds old.
- 5-minute candle: at most 480 seconds old.
- FlashAlpha structure: at most 360 seconds old and stamped `FRESH` upstream.

Both the candle and structure gates must pass. Otherwise the chart is greyed
and labeled `STALE — DO NOT INTERPRET`. With no candles and no structure record,
the panel shows the calm not-running state. Missing episode ledgers produce zero
markers rather than synthetic placeholders.

## Operator Launch

After the branch is deployed, start the read-only bridge separately. This does
not restart `scalp_server.py`:

```bash
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
./launch_market_structure_chart_v0.sh
```

The bridge listens only on `127.0.0.1:8423`. Keep that terminal open while the
dashboard is in use.
