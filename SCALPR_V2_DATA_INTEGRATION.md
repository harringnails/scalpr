# Scalpr V2 Data Integration Plan

## Decision

Build the intelligence platform beside the legacy trader. The first delivered
slice is read-only and paper/shadow-only: provider-neutral option-chain records,
an IVolatility EOD adapter, deterministic descriptive features, and append-only
local evidence. It has no broker import, order method, confidence score, or
position-sizing authority.

## Phase 1 delivered foundation

- `ivolatility_adapter.py` is disabled when `IVOLATILITY_API_KEY` is absent.
  The credential is injected at runtime, never persisted, returned, or included
  in an error. No capture loop starts automatically.
- `options_intelligence.py` normalizes provider data into versioned contracts
  and preserves missing fields explicitly.
- `options_feature_store.py` writes immutable raw, normalized, and feature
  artifacts under Git-ignored `v2_data/options/`, with content hashes and an
  append-only manifest.
- `options_capture_service.py` joins fetch, normalization, feature generation,
  and persistence for one explicit capture. Incomplete `PENDING` responses are
  retained as raw evidence but never promoted into a completed snapshot.
- `/api/v2/options-intelligence/status` reports capability and storage status.
  It does not call IVolatility or change trading state.

The initial evidence scope remains SPY options, 0-2 DTE. The EOD chain request
uses explicit -30% to +30% moneyness bounds because IVolatility's parameterized
chain endpoint requires a delta or moneyness range. Expanding that boundary
requires a versioned scope decision and replay tests.

## Metric integrity

Open interest and gamma support an **unsigned gamma concentration** measure.
They do not reveal whether dealers are net long or short each contract. Dealer
GEX, gamma-flip distance, and hedge pressure therefore remain `null` until a
documented, tested positioning model or a source with position-side evidence is
available. Scalpr must not dress an assumption up as observed institutional data.

The first features are descriptive only: contract counts, put/call OI and volume
ratios, OI-weighted IV by right, IV skew, relative spreads, and unsigned gamma
concentration. No recommendation is generated.

## Source roadmap

1. Validate the user's IVolatility entitlement against the EOD parameterized
   option-chain endpoint and record a sanitized capability matrix.
2. Add operator-triggered capture only after entitlement validation. Handle
   `PENDING` responses without treating them as completed evidence.
3. Add IV Rank, IV Percentile, IV/HV, term structure, and surface history from
   licensed endpoints, each with provider and receipt timestamps.
4. Retain Unusual Whales as the sole institutional-flow provider. Normalize it
   through `InstitutionalFlowProvider`; do not add a second overlapping flow
   feed unless a controlled benchmark proves unique incremental value.
5. Join Alpaca market events, technical features, macro events, and catalysts by
   event time with explicit missing/stale/degraded states.
6. Move the local store behind a repository interface before PostgreSQL or
   ClickHouse. Select the server database from measured retention/query needs,
   not architecture fashion.
7. Train only after frozen labels, leakage checks, walk-forward splits, class
   balance, costs/slippage, and an untouched out-of-sample cohort exist.

## Broker boundary

The future `BrokerAdapter` belongs to the paper-trading milestone, not the data
adapter. Its contract will cover submit/cancel/replace, positions/account, and
streams, but V2 will expose only a paper implementation until reconciliation,
partial-fill, restart, outage, and account-risk tests pass. Webull remains manual
or backup until a measured execution-quality benchmark supports a change.

## Required before any model recommendation

- Sufficient append-only history across market regimes.
- Frozen feature and label versions.
- Point-in-time joins with no future leakage.
- Walk-forward and untouched out-of-sample results.
- Fill, spread, latency, and slippage assumptions.
- Calibration and abstention thresholds.
- Independent deterministic risk controls.
- Human approval of a paper-only cohort; no automatic live path.

Official endpoint reference: <https://www.ivolatility.com/api/docs>
