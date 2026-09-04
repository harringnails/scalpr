# Scalpr Context Dashboard v0

The context panel in `dashboard.html` is an exploratory, non-inferential view of the latest point-in-time record in `market_context_shadow_v0.jsonl`. It has no controls and no execution, admission, pre-check, order, collector, or Guard authority.

Because `scalp_server.py` is frozen, a separate localhost-only read bridge exposes the latest record to the browser. The bridge accepts `GET /latest` and `GET /health`; all write methods return HTTP 405. It never writes to the ledger.

Start the bridge from the operative repository before opening the dashboard:

```bash
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
./launch_market_context_panel_v0.sh
```

Keep the existing operator-started `scalp_server.py` process unchanged. No restart is required when `dashboard.html` changes; refresh `http://127.0.0.1:8420/` in the browser.

The panel polls `http://127.0.0.1:8421/latest` every 30 seconds. A missing or empty ledger has zero observations and renders the calm `CONTEXT CAPTURE NOT RUNNING` state. After at least one observation exists, a latest record older than 120 seconds or stamped `STALE` renders `STALE — DO NOT INTERPRET` with blank readings. The composite and weights remain unscored until their parameters are frozen.

## Health audio

The `Enable health alerts` toggle is off by default and may persist locally in the browser. Once enabled by a user gesture, a quiet descending two-note health tone fires on a transition from live (`CLEAN` or `DEGRADED`) to `STALE`. It does not fire for a missing or empty ledger. It fires once per stale transition and may repeat only after 15 minutes of continuous staleness. This is a data-health/awareness alert only; it has no trade meaning and no execution, admission, pre-check, order, or Guard connection.

## Future EDGE verdict tone (specification only)

Do not implement this trigger in the context panel. A future, separate study-verdict integration may emit a visibly prominent alert and a sound distinct from the quiet descending STALE health tone only when the authoritative frozen study machinery computes the literal verdict `EDGE` after all of these gates pass:

- The pre-registered sample size `N` is met.
- The session-block matched-null threshold passes.
- All four chronological walk-forward folds pass the frozen sign rule.

An exploratory context state, provisional composite, per-poll reading, candidate, or in-sample result must never trigger the EDGE tone. Implementation belongs with the future frozen study-verdict machinery after calibration, operator approval, parameter freeze, and prospective loggers exist.
