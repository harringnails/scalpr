# Executable-Bid Freshness Audit v0 (for the record / Codex)

**Status: preliminary, evidence-based. No code changed by this note. Paper/shadow, prelock dry run.** One actionable fix specified; one non-issue flagged so it isn't re-chased; one structural finding to sit with before any lock.

## Scope (read this first)

This audit is the **single tracked CALL's outcome window** — the one qualifying setup of the whole dry run (`decided_at 2026-08-11T18:53Z`, ~1 hour, **670 bid ticks on one contract**). It is *indicative, not conclusive*: a fuller audit across more contracts/sessions is needed to generalize. But the two buckets below separate cleanly, and the artifactual one is unambiguous.

## What the ticks show

670 ticks: **FRESH 437 (65.2%), STALE 143 (21.3%), UNUSABLE 90 (13.4%)**. That 65% FRESH is what dropped the outcome below the coverage bar → `UNLABELABLE_INSUFFICIENT_COVERAGE`.

`quote_age_seconds`: min −2.84, p50 1.54, p90 9.73, p99 41.8, max 44.87.

| status | n | age p50 | age p90 | age max | reason |
|---|---|---|---|---|---|
| FRESH | 437 | 1.28 | 3.59 | 4.95 | within ≤5s — genuinely fresh |
| STALE | 143 | 8.84 | 35.44 | 44.87 | quote age > threshold |
| UNUSABLE | 90 | −0.08 | −0.02 | −0.01 | `PROVIDER_TIME_AFTER_RECEIPT` (negative age) |

Poll cadence (gaps between consecutive `observed_at`): p50 5.18s, p90 8.21s, p99 12.09s, **max 18.5s**; only 2 gaps >15s, 0 >30s. STALE/UNUSABLE ticks are spread across **58 of 60 minutes**, not clustered in fast-tape bursts.

## Finding 1 — UNUSABLE (90, 13%) is a timestamp/clock ARTIFACT. Fix it.

Every UNUSABLE tick is `PROVIDER_TIME_AFTER_RECEIPT` with a **negative** age (−0.01 to −2.84s). The quotes are two-sided and current; they are rejected only because the provider's `observed_at` lands a fraction of a second *after* our `received_at` — clock skew between the provider/exchange timestamp and our receipt stamp, not stale or bad data.

**Fix (in `entry_bid_capture_v1.py`, `build_bid_record`):** the guard `if age < 0: reasons.append("PROVIDER_TIME_AFTER_RECEIPT")` (with the status mapping to `UNUSABLE`) is too strict. Introduce a small, configured **provider-ahead tolerance** (e.g. `clock_skew_tolerance_seconds`, ~3s):

- If `-tolerance <= age < 0`: treat as **FRESH**, clamp effective age to `max(0.0, age)` for the freshness decision. Still record the raw signed `quote_age_seconds` for observability, and optionally a `clock_skew_clamped: true` flag.
- Only `age < -tolerance` (a genuinely anomalous "from the future" quote) stays `UNUSABLE` / `PROVIDER_TIME_AFTER_RECEIPT`.

**Effect:** recovers all 90 ticks → FRESH rises **65.2% → ~78.7%** on this window, no threshold loosening. This is unambiguous; do it regardless of the strategic questions.

## Finding 2 — cadence/batching is FINE. Do NOT re-chase it.

Poll gaps are ~5s (p50 5.18s) with a small tail (max 18.5s, only 2 gaps >15s). The STALE **ages** (up to 45s) are far larger than any poll gap, so staleness is **not** caused by us polling slowly. Batching the fetch or tightening cadence would be effort against a non-issue. Explicitly de-scoped.

## Finding 3 — the residual STALE (143, 21%) is largely REAL, and it's the strategic one.

STALE ages run 8–45s and are spread across nearly every minute of the hour (not fast-tape bursts). Cadence is fine. The honest reading: the **single 0–2 DTE option contract's own latest quote genuinely goes stale during quiet stretches** — the strike isn't being quoted every 5s when the tape is calm. That is real market microstructure, not a collector bug, and no fetch change fixes it.

Consequence, and this links freshness back to sparsity: even *with* the clock fix, this window sits ~79% FRESH — still under the 80% coverage bar. So a clean fire on a clean contract can **still** fail to label because the contract lacks dense enough quote flow. It is not only "setups are rare"; it may be "the rare ones sit on contracts without continuous enough quotes to score."

## Decisions this forces (not to be papered over)

1. **Ship the clock-tolerance fix** (Finding 1). Free win.
2. **Leave cadence alone** (Finding 2).
3. **Do NOT loosen the `≤5s` freshness threshold** to rescue coverage — that would relabel real staleness as fresh and corrupt the outcome basis. If real quote gaps are structural, the honest options are: (a) accept lower coverage with wider confidence intervals rather than a looser freshness definition, or (b) reconsider whether single-option executable-bid outcome tracking is viable on thinly-quoted 0–2 DTE strikes at all. Both are design decisions for a future frozen cohort, not tweaks.

## Bottom line

The freshness loss is **~40% artifactual (clock, fixable now) and ~60% real (option-quote sparsity, structural)**. Fix the clock, ignore batching, and treat the residual as a genuine viability question that — together with setup rarity (~4 fires / 6 sessions) — belongs in the v2-fork decision, not in a lock of the current cohort.
