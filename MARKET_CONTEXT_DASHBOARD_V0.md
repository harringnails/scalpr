# Scalpr Context Dashboard v0

The context panel in `dashboard.html` is an exploratory, non-inferential view of the latest point-in-time record in `market_context_shadow_v0.jsonl`. It has no controls and no execution, admission, pre-check, order, collector, or Guard authority.

Because `scalp_server.py` is frozen, a separate localhost-only read bridge exposes the latest record to the browser. The bridge accepts `GET /latest` and `GET /health`; all write methods return HTTP 405. It never writes to the ledger.

Start the bridge from the operative repository before opening the dashboard:

```bash
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
./launch_market_context_panel_v0.sh
```

Keep the existing operator-started `scalp_server.py` process unchanged. No restart is required when `dashboard.html` changes; refresh `http://127.0.0.1:8420/` in the browser.

The panel polls `http://127.0.0.1:8421/latest` every 30 seconds. Records older than 120 seconds, records stamped `STALE`, missing ledgers, empty ledgers, and bridge failures all render as `STALE — DO NOT INTERPRET` with blank readings. The composite and weights remain unscored until their parameters are frozen.
