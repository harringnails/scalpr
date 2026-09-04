# Signal Episode Loggers v0 — Operator Runbook

**Status:** Paper/shadow, read-only over source data, append-only to the two isolated study ledgers. These commands have no execution, admission, order, collector, server, Guard, A2, or dense-store wiring.

## Freeze boundary

The v0 preregistrations were frozen on 2026-09-04. Sessions on or before that date are in-sample and are logged as excluded. Prospective accrual begins with the next full RTH session after the freeze commit.

## Daily evaluation

Run after the 60-minute outcome endpoint is available. Replace `YYYY-MM-DD` with the session date.

```bash
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"

PY=".venv/bin/python"
FLASH_LEDGER="/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-flashalpha-pin-scanner/flashalpha_pin_study_v0.jsonl"

"$PY" prior_regime_flip_reclaim_logger_v0.py evaluate \
  --session-date YYYY-MM-DD \
  --tick-log tick_log.csv \
  --flashalpha-ledger "$FLASH_LEDGER"

"$PY" intraday_continuation_logger_v0.py evaluate \
  --session-date YYYY-MM-DD \
  --tick-log tick_log.csv \
  --flashalpha-ledger "$FLASH_LEDGER"
```

Default outputs are `prior_regime_flip_reclaim_v0.jsonl` and `intraday_continuation_v0.jsonl`. Both are covered by the repository's `*.jsonl` ignore rule. Each evaluator appends at most one record per study/session; rerunning the same date is idempotent. Missing or stale source data produces an explicit exclusion record rather than an imputed episode.

## Interpretation

Every record remains `UNDERPOWERED` until separate frozen verdict machinery has the required N, matched null, and chronological fold results. An individual record is not an edge verdict or a trade signal.
