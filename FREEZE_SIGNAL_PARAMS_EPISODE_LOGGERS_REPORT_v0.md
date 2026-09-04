# Freeze Signal Parameters + Episode Loggers — v0 Report

**Freeze date:** 2026-09-04

**Safety ancestry:** real `safety/pre-cleanup-snapshot`, including `8ea24267`

**Scope:** paper/shadow episode detection and isolated JSONL accrual only

## Frozen preregistration hashes

| Study | Frozen preregistration | SHA-256 |
|---|---|---|
| Study A | `PREREG_prior_regime_flip_reclaim_v0.md` | `7c809b1ae217b08995df0a6229e36b67d4ed710f427fd35c8c323e090ae811fb` |
| Study B | `PREREG_intraday_continuation_v0.md` | `0210c057037bb5a5b8d4be82c770ed73224cdbad5d373c3bf081f66c6dae78d6` |

Every episode or exclusion record stamps its study's runtime-computed preregistration SHA-256. Any parameter change requires a new study ID and resets N to zero.

## Frozen values

- Study A: `A=0.20`, `W=900 s`, `G=120 s`, `N=150` per H1a/H1b cohort, `p<=0.01`, four chronological folds with at least 3/4 sign-consistent.
- Study B: `S=0.25`, `R=180 s`, `V=2 min`, at least three consecutive positive one-minute returns, executed volume excluded, `N=150`, `p<=0.01`, four chronological folds with at least 3/4 sign-consistent.

## Implementation boundary

- The loggers read point-in-time SPY quotes and FlashAlpha shadow records, then write only their own ignored JSONL ledgers.
- Outcome points use clean two-sided SPY mids and the frozen at-or-before anchor / at-or-after endpoint rule with a maximum five-second offset.
- Missing, stale, crossed, zero, or incomplete data is never imputed.
- Study B labels its VWAP calculation as a quote-mid proxy and does not use a volume field.
- No live execution, admission, order, collector, server, Guard, A2, or dense-store path is imported or changed.
- `scalp_server.py` safety and branch SHA-256: `ba20c7cd084825a57a2326dbafe98b89c28fb5a76e23ec8659d8d94e362838e7`.
