# Reversal v2 — Near-Miss Feasibility / Margin Report v0

**Purpose:** a **timeline feasibility estimate only.** It answers "would these variants fire often enough to reach 200 episodes in a reasonable window?" — nothing more.

**What this report is NOT:** it does **not** select, tune, or alter any threshold. The V2-A…D definitions are frozen a-priori in `REVERSAL_V2_RELAXATION_OPTIONS_v0.md` and stand regardless of these numbers. These counts are computed on recent (already-seen) sessions and are therefore **in-sample** — usable for planning, never for the inferential edge test, which runs only on post-freeze prospective data.

## Method

Over the four most recent sessions with data (2026-08-17 → 2026-08-20), for each evaluated (non-warmup, direction status `FRESH`) minute×side candidate, tally which gates it failed. These give **upper-bound ceilings** per variant — full relaxation of a gate. A *mild* relaxation (which is what V2-A…C are) recovers only a fraction of its ceiling, so real fire rates land **below** these numbers. The exact rate is confirmed prospectively.

## Observed near-miss counts (per session)

| Session | Evaluated | fails **only** momentum | fails **only** causal | fails only {momentum,causal} | atr-fail w/ multiple in [1.25,1.5) |
|---|---:|---:|---:|---:|---:|
| 2026-08-17 | 572 | 5 | 41 | 255 | 36 |
| 2026-08-18 | 596 | 54 | 40 | 268 | 54 |
| 2026-08-19 | 600 | 54 | 60 | 311 | 50 |
| 2026-08-20 | 586 | 20 | 80 | 245 | 60 |

## Reading it — ceilings per variant (upper bounds)

- **V2-A (momentum RSI-turn tolerance):** ceiling ≈ "fails only momentum" ≈ **~5–54/session**. A mild RSI-band/"stops-falling" widening recovers a fraction of these — plausibly low-single-digits to low-tens per session.
- **V2-B (causal reclaim window, ±2–3 bars):** ceiling ≈ "fails only causal" ≈ **~40–80/session** — the largest single-gate headroom.
- **V2-C (single decelerating bar):** adds to the momentum recovery; part of the ~5–54 "only momentum" pool.
- **V2-D (atr 1.5→1.25 + V2-A):** the atr near-miss pool in [1.25,1.5) is **~36–60/session**, on top of the V2-A recovery.
- **Aggressive reference (both binding gates fully relaxed):** ~**245–311/session** — shown only to bound the space; not a variant we'd deploy without the milder ones failing first.

## Feasibility conclusion

Every variant's ceiling is **far above** the ~2 non-overlapping qualifying fires/session needed to reach 200 in ~3–4 months. Even recovering a modest fraction of the mildest variant (V2-A) clears that bar. So **fire rate is not the binding constraint** — reaching studyability in a few months is comfortably achievable, and the counts are stable across the four sessions (not a one-day artifact).

The binding question remains **edge, not volume**: these recovered candidates are precisely the ones that missed the clean-turning-point criteria, so the real risk is dilution — which is exactly why the frozen plan tests mildest-first and requires a post-freeze prospective verdict per variant.

## Discipline restatement

- These numbers are **planning-only**, in-sample, and do not touch the frozen thresholds.
- The inferential edge test uses **post-freeze prospective** data with the session-block matched null + 4-fold walk-forward, per variant, mildest-first.
- v1 remains untouched; implementation waits on platform hardening + dense-source labeling.
