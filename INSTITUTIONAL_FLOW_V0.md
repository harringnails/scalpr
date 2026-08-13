# Institutional Flow V0

## Provider decision

Unusual Whales is the configured and only institutional-flow provider. No
second real-time flow adapter exists or is planned. Another provider may
be evaluated later only with measured latency, uptime, classification,
licensing, cost, and incremental-predictive-value evidence.

Unusual Whales and IVolatility have separate roles:

- Unusual Whales: current options flow, aggressor activity, alerts, and dark
  pool context.
- IVolatility: point-in-time option chains, volatility state, Greeks, surfaces,
  skew, and historical research.

## Delivered boundary

- `InstitutionalFlowEvent` preserves provider event time and local receipt time,
  raw provider payload, deterministic identity, contract fields, aggressor side,
  and explicitly uncalibrated source confidence.
- `InstitutionalFlowProvider` is the neutral async interface.
- `UnusualWhalesAdapter` retrieves recent REST flow alerts outside the trading
  loop, normalizes UTC timestamps, retries transient failures, handles 429s,
  rejects malformed rows, deduplicates with a TTL, and reports health metrics.
- `InstitutionalFlowStore` creates the four versioned local tables for events,
  snapshots, health, and ingestion errors. SQLite is a local migration store;
  the contract can later move behind PostgreSQL or ClickHouse.
- `InstitutionalFlowIngestionService` performs one explicit fetch/persist/
  aggregate/health pass. It records unavailable snapshots on provider failure
  and has no scheduler or execution path.
- Rolling snapshots are deterministic for 1/5/15/30-minute windows. Scores use
  0..1 for magnitudes and -1..+1 for direction. Empty input produces
  `UNAVAILABLE` with null scores—not zeros.

The adapter is enabled for bounded shadow capture under
`institutional-flow-config-v1`: SPY is polled at most once per minute during the
regular session from a worker isolated from Guard/order execution. The existing
`UW_API_KEY` Keychain/environment flow remains authoritative; tokens
are never stored in events, errors, status responses, fixtures, or source.

## Interpretation limits

Flow is descriptive evidence. It cannot qualify a trade, submit an order,
authorize an exit, or independently increase position size. Aggregated UW alert
fields do not prove whether a position is opening unless the provider explicitly
supplies that classification, so `opening_position_probability` remains null.
Likewise, a missing event window is unknown rather than neutral.

The initial fusion weights are versioned in `CONFIDENCE_WEIGHTS`, but no overall
confidence calculation is enabled. Weight calibration requires replay, ablation,
walk-forward validation, and an untouched paper/shadow cohort.

## Licensing and retention

Raw payloads remain private and local. The default configuration proposes seven
days for raw data and 90 days for normalized data, but automated deletion is not
enabled until the applicable Unusual Whales retention and redistribution terms
are verified. Scalpr should expose derived scores, not vendor payloads, to any
future external customer.

Official schema references:

- <https://api.unusualwhales.com/docs/kafka/types/FlowAlert>
- <https://api.unusualwhales.com/docs/kafka/types/OptionTrade>
- <https://api.unusualwhales.com/docs/kafka>
