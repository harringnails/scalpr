# Scalpr Testing

## Current baseline

The repository contains 103 test functions across 13 standalone `test_*.py` scripts. The baseline scripts passed individually with the local Python runtime. `pytest` was not installed, so `python3 -m pytest -q` could not run.

## Current command

From `Scalpr7/`:

```bash
for file in test_*.py; do python3 "$file" || exit 1; done
```

Tests must use fakes/fixtures and must not require credentials or live network access.

## Coverage strengths

- Event-time bar boundaries, lateness, deduplication, and replay determinism.
- Intelligence feature schema, scoring separation, and label lifecycle.
- Ratchet whole-position exit behavior and known anomalous-peak failure.
- Wave state transitions, restart recovery, idempotency, quote gates, and live-adapter isolation.
- Incubation materiality, factorial cells, lifecycle, and cohort reporting.
- Flow-evidence status and agreement rules.

## Missing test infrastructure

- Locked dependencies and supported Python version.
- Standard pytest configuration and markers.
- CI for tests, linting, type checks, secret scanning, and large-file checks.
- API contract tests and authentication tests.
- Broker reconciliation, partial-fill, restart, outage, and account-risk scenarios.
- Golden replay parity tests between legacy and V2.

## V2 acceptance rule

Every migrated module needs typed input/output contracts, deterministic fixtures, replay parity evidence, and explicit tests for missing/stale/degraded data. Execution-related tests use a paper-broker fake only.
