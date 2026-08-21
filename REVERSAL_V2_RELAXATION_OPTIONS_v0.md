# Reversal v2 — Relaxation Options (Pre-Registration v0)

> **🔒 FROZEN — 2026-08-20, before any outcome or edge result has been reviewed.** The four variant definitions (V2-A…D) and their thresholds are **a-priori and final**; the capped set will not be expanded or re-tuned. Any change after this date is a **new** pre-registration, not an edit. Immutability anchor = the git commit of this file. The separate feasibility/margin report (`REVERSAL_V2_FEASIBILITY_MARGIN_REPORT_v0.md`) is planning-only and does **not** alter any threshold here. v1 stays untouched; implementation is deferred until platform hardening and dense-source labeling are complete; V2-A is then validated only on **post-freeze** data.

**Status:** pre-registered design spec. Paper/shadow. **New frozen cohorts — does not touch the frozen v1 cohort, its thresholds, or its data.** No implementation yet; this is the a-priori variant set, now frozen before any outcome is seen. Governed by `REVERSAL_V2_DESIGN_BRIEF.md` (meta-discipline) and the frozen A2 basis + Phase-4 edge test.

**Honest framing (read first):** this makes the signal *studyable*, not profitable. Every variant below may reach 200 episodes and show **NO EDGE** — that is a legitimate, useful result (redirect to order-flow signals), not a failure. No relaxation is guaranteed to produce edge; the point is a trustworthy verdict in months instead of years.

## The bottleneck (from the 2026-08-20 funnel, 720 evaluations)

Of the 586 evaluated (non-warmup) minute×side candidates, the four AND-ed gates passed at:

| Gate | Pass rate | Binding? |
|---|---|---|
| `level_proximity` | ~100% | no (SPY is always near a level) |
| `atr_extension` (≥1.5×ATR from extreme) | 42% | secondary |
| `causal_confirmation` (higher-structure OR level/VWAP reclaim *this bar*) | 26% | **yes** |
| `momentum_slowing` (RSI-turn-from-oversold OR two decelerating directional bars) | 16% | **yes (tightest)** |

All four must hold in the same minute → **0 qualified.** And `atr_extension` (an extended, still-moving push) and `momentum_slowing` (that push decelerating) are near-opposite states, so the joint is rarer than independence predicts — the setup hunts the exact turning point, which is intrinsically rare.

## Key insight: fire rate is easy, edge preservation is the whole problem

Mechanical ceilings from today (full relaxation = drop the gate): relaxing `causal_confirmation` alone would qualify **~80/session**, `momentum_slowing` alone **~20**, both together **~245**. Even a small fraction of these overshoots the ~2/session needed to reach 200 in ~3–4 months. **So we are not fighting for fires — we are trying to keep edge while admitting the fewest non-turning-point fires.** Strategy: enumerate small relaxations, test **mildest-first**, stop at the first variant that is both studyable *and* shows edge.

## The exact relaxation levers (what each gate actually checks)

- `momentum_slowing = range_contract OR rsi_turn`, where `rsi_turn` = RSI was past the 35/65 threshold and is now reversing, and `range_contract` = two consecutive directional bars with the current range smaller than the prior.
- `causal_confirmation = higher_structure OR reclaim-this-bar`, where a reclaim requires price to cross back through a level/VWAP on the *current* bar only.

## Capped, ordered variant set (freeze all before collecting; test in this order)

Each is a **separate frozen cohort** with its own `cohort_id`/hash, validated only on data collected **after** it is frozen (never on the funnel above). Capped at four for multiple-comparison control (§J).

- **V2-A — mildest (RSI-turn tolerance).** `rsi_turn` accepts `current_rsi >= prior_rsi` (RSI *stops falling*) and widens the band 35→40 / 65→60. Keeps the "momentum stalling near an extreme" spirit; smallest step.
- **V2-B — reclaim window.** `causal_confirmation` accepts a level/VWAP reclaim within the last **2–3 bars**, not only the current bar. Keeps "confirmed reversal," drops the exact-minute requirement.
- **V2-C — decelerating range.** `range_contract` accepts a single directional bar with range < prior (drops the "two consecutive" requirement).
- **V2-D — extension + mildest.** `atr_extension` 1.5→1.25 combined with V2-A only. (Today's near-misses cluster just under 1.5, e.g. observed 1.357.)

Order rationale: A→D is increasing looseness = increasing dilution risk. Test mildest-first so that **if** edge exists, it's found before it's diluted away.

## Validation protocol (per variant)

- New frozen cohort; **prospective** accrual only; the variant never validated on data it was shaped by.
- A2 basis (signed `return_60m`), **dense-source labeled** (requires the dense-source fix live), non-overlap dedup.
- Session-block matched null (`p=(k+1)/(n+1)`) + 4-fold chronological walk-forward; verdict EDGE / NO EDGE / UNDERPOWERED at n≥200.
- **Mildest-first, stop-on-first-edge.** A failed variant → advance to the next *pre-registered* variant; **never re-tune the failed one** on its own data.
- Multiple-comparison accounting across the (≤4) variants: pre-registered order with alpha-spending, so testing four doesn't manufacture a false positive.
- v1 keeps running as the strict reference cohort (unchanged).

## Rough timeline (feasibility, not a threshold-choice basis)

At even a conservative ~2–4 qualifying fires/session, 200 non-overlapping episodes ≈ **~50–100 sessions ≈ 3–5 months per variant** — vs the multi-year v1 wait. The exact per-variant rate is confirmed prospectively; the ceilings above only establish it is achievable, and are **not** used to pick thresholds.

## Sequencing / dependencies

1. This pre-registration can be frozen **now** (paper, no code).
2. Implementation is gated behind: platform stability cleanup complete (don't add detector complexity to an unstable server) and the **dense-source A2 fix live** (so the new fires actually label).
3. Then implement V2-A as a new cohort, accrue prospectively, run the Phase-4 harness at n≥200.

## Discipline / non-negotiables

- New cohorts only; frozen v1 untouched. Thresholds chosen a-priori (principled small steps), **not** fit to the funnel. Variant set capped and pre-registered; failures advance, never re-tune. Missing stays missing. Paper/shadow; no Guard/order authority.
- **No edge guarantee.** If all four variants clear the studyability bar and none shows edge, the honest conclusion is that the reversal concept lacks tradeable directional edge even relaxed — and effort redirects to the denser order-flow studies. That outcome is a success of the method, not a failure of it.
