# Scalpr V2 Roadmap

## M0 - Sanitized baseline

- Establish Git boundary and ignore private/generated data.
- Record product, architecture, current state, decisions, risk, testing, and runbook.
- Verify all legacy tests without network access.

Exit: reproducible source baseline with no credentials or raw trading data tracked.

## M1 - Contracts and replay harness

- Add locked Python environment and CI.
- Define typed market event, quote, bar, feature, thesis, risk, order, and audit-event schemas.
- Build immutable manifest/hashing for legacy evidence.
- Add golden replay fixtures for SPY 0-2 DTE.

Exit: deterministic legacy outputs can be reproduced from sanitized fixtures.

## M2 - Data and deterministic analytics

- Implement centralized IEX/OPRA adapters and batching/rate-limit control.
- Normalize the existing Unusual Whales integration behind the provider-neutral
  institutional-flow contract; preserve event/receipt time and deduplicate.
- Integrate IVolatility for point-in-time options-state history after entitlement
  validation. Do not add a second overlapping institutional-flow provider.
- Separate append-only capture from derived storage.
- Migrate bar, feed-quality, premarket, feature, regime, and flow calculations.

Exit: V2 matches approved legacy outputs or records versioned differences.

## M3 - Persistent paper trading and risk

- Add paper-only broker adapter and expected-account verification.
- Implement persistent order/position/Guard state and startup reconciliation.
- Add deterministic account, quote-quality, liquidity, and emergency risk controls.
- Wire pause/resume audit events.

Exit: failure/restart/partial-fill simulations pass; no live configuration exists.

## M4 - Operator API and dashboard

- Move policy out of UI/API routes.
- Rebuild retained workflows with authenticated state-changing actions.
- Make data quality, evidence status, paper/shadow mode, and risk blocks prominent.

Exit: operator workflows pass end-to-end paper tests.

## M5 - AI thesis monitor

- Define structured AI input/output schema.
- Add evidence/contradiction/missing-data/posture/invalidation output.
- Persist prompts, model versions, snapshots, and human actions.
- Run advisory shadow evaluations only.

Exit: deterministic calculations and risk remain independent; AI never submits orders.

## M6 - Parallel validation and cutover

- Run legacy and V2 side by side.
- Compare data completeness, decisions, latency, simulated fills, and failure behavior.
- Freeze a paper cohort before tuning.

Exit: approved paper-only cutover with rollback artifacts. Live use remains out of scope.
