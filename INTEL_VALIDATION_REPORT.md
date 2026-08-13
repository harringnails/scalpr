# Scalpr Intelligence — Phase 1 Validation Report

*Generated 2026-07-29T15:00:52.236356+00:00 · `scalpr-intel-v0` · **operational only**, non-qualifying (no ML, no probabilities, no edge claim).*

**Sessions collected: 0 / 7**  — *awaiting live sessions; metrics below are the current (near-empty) state of a freshly-activated pipeline.*

## Pipeline volume

- Snapshots created: **0**
- Duplicate snapshots prevented: **0**
- Contracts frozen (complete in-band universe): **0**

## Label states

- (no labels yet)

## Pending age (hours)

- median: **None** · max: **None** · pending labels: 0

## Quote quality

- contracts assessed: **0**
- stale-quote rate: **None** · bad/locked-quote rate: **None**

## Missing-data rates by feature (null fraction across snapshots)

- (no snapshots yet)

## Label outcomes by quote-quality bucket

| bucket | contracts | FINAL | target-before-stop | other states |
|---|--:|--:|--:|---|
| high_quality | 0 | 0 | — | — |
| warning_quality | 0 | 0 | — | — |
| locked | 0 | 0 | — | — |
| stale_unlabelable | 0 | 0 | — | — |
| unusable_unlabelable | 0 | 0 | — | — |

*high_quality = clean quote · warning_quality = minor-stale (>15m) but labelable · locked = bid==ask · stale_unlabelable = materially stale (>60m) → UNLABELABLE_STALE · unusable_unlabelable = crossed/missing/unusable. Contracts stay frozen in the universe even when unlabelable.*

## Lifecycle health

- lifecycle errors (ERROR_RETRYABLE): **0**
- proxy target/stop collisions (ambiguous_same_bar): **0**

## Rules-score decision distribution

- (no snapshots yet)

---

Operational health only — verifies the data-generation machinery, not profitability or edge. Delta-gamma proxy labels; no ML; no calibrated probabilities. Non-qualifying (formal_cohort_eligible=false).
