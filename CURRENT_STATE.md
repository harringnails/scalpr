# Scalpr Current State

Verified 2026-08-02 against the authoritative root folder and running local service.

## Runtime

- Python service listening on `127.0.0.1:8420`.
- Dashboard reported PAPER mode, no guarded positions, and no open positions in the connected Alpaca paper account.
- Underlying data uses IEX; options use OPRA. SIP appears only in feed validation.
- Shadow observers have experienced HTTP 429 rate limits because requests are not centrally batched.

## Evidence snapshot

- Journal: 178 paper trades; 80 wins / 98 losses; 44.9% win rate; computed net P&L `-$20,739`.
- 2026-07-31: 10 trades, 80% wins, mean realized `+8.632%`; excluding the `-95.7%` trade, mean `+20.222%`.
- All seven July 31 trades with peak at least 15% finished positive; mean realized `+26.484%`.
- Wave Cohort A reports zero additions across 33 simulations.
- Raw evidence remains local and Git-ignored.

## Implemented

- Manual paper entries and whole-position ratchet exits.
- Pre-trade checklist, journal analytics, workup, premarket scorecard, regime research, feed/timing checks.
- Versioned intelligence snapshots and outcome lifecycle.
- Wave Riding and incubation shadow/replay infrastructure.
- Local dashboard and read-only research endpoints.

## Partial or conflicting

- A `--live` code path exists despite the product not being approved for live capital.
- `guard_events.py` exists but pause/resume endpoints do not call it.
- Documentation contains older cohort counts and a stale 43.6% win-rate statement.
- Tests exist as standalone scripts but there is no dependency lock, standard test runner configuration, or CI.
- The parent Git repository exists but has no baseline commit.

## Missing before any execution-grade system

- Durable Guard/position state and startup broker reconciliation.
- Broker-side protection and emergency exit separation.
- Websocket-reconciled order lifecycle and partial-fill handling.
- Idempotent client order IDs.
- Account-level exposure, daily-loss, concentration, pending-order, and Greeks limits.
- Quote freshness/liquidity/slippage enforcement in the execution path.
- Authentication/CSRF protection for state-changing APIs.
- Encrypted credential storage and hard account/environment separation.
- Structured logging, dependency locking, migrations, CI, and disaster recovery.
- A production AI interpretation layer.

