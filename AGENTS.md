# Scalpr Engineering Rules

This file governs all work in `Scalpr7/`.

## Authority and scope

- The loose files in this directory are authoritative. `Scalpr7 Compressed/` and the ZIP are historical only.
- V2 begins paper/shadow only. Do not enable, extend, test against, or prepare a real-money order path.
- The automated/default V2 scope is SPY options with 0-2 DTE. ADR-019 records a
  separate manual-paper-only 0-60 DTE equity-option path; it must never be used
  by automated systems or validated research cohorts.
- Deployment is local-first. Keep interfaces compatible with a later always-on capture worker.

## Safety

- Never read, print, copy, stage, commit, upload, or migrate `scalp.keys` or any `.keys`/`.env` file.
- Never weaken risk checks or observational/live isolation.
- Do not modify the running legacy service unless the task explicitly targets it and the impact is stated first.
- Treat broker calls, order submission, liquidation, pause/resume, and feature flags as behavior-changing operations.
- Fail closed when execution data, account identity, market status, or quote quality cannot be verified.

## Frozen evidence contracts

Do not silently change these. Any change requires a new version and cohort:

- Incubation hash `3425809bb0a3200fef8439dbcd5791d877cf4fe9e998c5f0cc41f8b100007eed`.
- Incubation primary cells and 10/10/15 pp materiality on executable option bid.
- `scalpr-intel-v0`, including 900-second warning and 3600-second unlabelable thresholds.
- `wave-riding-v0`, `intraday-atr-v0`, and `wave-riding-baseline-v0`.
- `flow-evidence-v0` GREEN requirements.
- The legacy Guard has no separate hard stop. That is a current-state fact, not approval to omit a V2 emergency risk design.

## Data and reproducibility

- Raw CSV/JSON/JSONL and generated state are local evidence, not source code; keep them ignored by Git.
- Never rewrite historical records in place. Migrations write versioned derivatives with provenance and hashes.
- Deterministic calculations stay outside the AI interpretation layer.
- Missing, stale, degraded, or unavailable inputs must remain explicit; never substitute neutral values.

## Verification

- Keep changes narrowly scoped and add tests for changed behavior.
- Run the affected test scripts after each milestone; run all repository tests before a baseline or release commit.
- Tests must not make live broker calls or require real credentials.
- Every handoff states files changed, tests passing/failing, live/risk impact, and remaining risks.
