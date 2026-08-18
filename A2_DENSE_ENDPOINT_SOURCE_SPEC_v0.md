# A2 Dense-Endpoint-Source Fix + Capture-Gap Bias Check — Pre-Registered Spec v0

**Status:** pre-registered measurement-infrastructure fix. Paper/shadow, research-only. No admission/Guard/order impact. Amends how `a2_measurement` sources SPY horizon endpoints; **does not change the A2 basis definition, the freshness rule, or any threshold.**

## Why (the objective finding, independent of any episode's outcome)

Live ground truth for 2026-08-14: the SPY underlying capture the A2 labeler reads (`tick_log.csv`) has **944 inter-quote gaps > 5s** in the day and a **~2.5-minute void (16:09–16:11:30 UTC / 12:09–12:11 ET)** — mid-session, when SPY is maximally liquid. Median cadence is a healthy 2.1s, but the gap *tail* is what rejects episodes. Today's admitted episode was marked `A2-UNAVAILABLE` because its 15-minute endpoint fell inside such a gap. This is a **data-capture defect, not market illiquidity and not the endpoint rule.**

This fix is adopted because the capture is demonstrably gappy — an objective defect that motivates the change **regardless of whether today's episode passed.** It is applied uniformly to all episodes; it is not a change made to rescue one episode.

## Part 1 — Dense endpoint source (same basis, denser source)

Source A2 anchor and horizon endpoints for SPY from **Alpaca historical stock quotes** (`data.alpaca.markets`, SIP), not the incidental live `tick_log`. SPY historical quotes are sub-second and effectively gap-free in RTH.

- **Basis unchanged:** still a **two-sided mid** = (bid+ask)/2 from a fresh, non-crossed quote. Historical stock quotes carry bid/ask, so the mid definition is identical — only the source changes. (Do **not** substitute trade prints or OHLC bars; those are not two-sided mids.)
- **Freshness rule unchanged — NOT loosened.** Keep the `≤ 5s`, two-sided, non-crossed requirement with the shipped clock-skew tolerance. A denser source means the same bar is *met legitimately*, far more often. This is explicitly **not** a threshold relaxation.
- **Point-in-time correctness (no look-ahead):** each endpoint = the last fresh SPY quote **at or before** its boundary timestamp, within the freshness tolerance. Anchor = at or before `t0`. Pulling endpoints historically (after the horizon closes, T+60m+) introduces no look-ahead because each is a point-in-time price at its own boundary.
- **Missing stays missing (§K):** if even the dense source lacks a fresh quote at a boundary (rare for SPY), that endpoint is `UNAVAILABLE`, recorded with reason — never imputed.
- **Provenance:** stamp every endpoint with `endpoint_source` (`alpaca_historical_stock_quote_v1` vs legacy `live_tick_log`), the provider timestamp, and age. Keep the live tick_log as a cross-check, not the primary.
- **Authenticated pull:** operator Terminal (Keychain), not an agent shell.

**Uniform re-label:** because A2 has accrued ~0 clean episodes (not yet an inferential set), re-label all admitted, non-quarantined, non-collision episodes from the dense source under the identical basis/freshness rule. Every episode's status is then determined by the corrected source, uniformly — including today's. This is principled (uniform, source-defect-driven), not a cherry-pick.

## Part 2 — Capture-gap bias check (characterization only)

Determine whether the `tick_log` capture gaps are **random** (old basis just slower) or **condition-correlated** (old basis biased — a validity threat, since labelable episodes would over-represent calm windows).

- **Sample:** the recent RTH sessions with tick_log coverage.
- **Identify gaps:** every SPY-quote inter-arrival `> 5s` in tick_log during RTH (start, duration).
- **Activity proxy per minute** (from the dense source, so it's itself gap-free): SPY 1-minute absolute return and quote/trade intensity, plus 5-minute ATR.
- **Test:** compare the activity distribution of **gap-minutes** vs **clean-minutes** (e.g., Mann-Whitney U, with effect size), and report the fraction of gaps falling in the top activity quartile.
- **Verdict:** `GAPS_RANDOM` (no material activity correlation → old basis unbiased, only slower) vs `GAPS_CONDITION_CORRELATED` (gaps cluster in fast/high-volume minutes → old tick_log basis biased; any prior tick_log-derived coverage stats — including the Stage A freshness reads — are suspect and should be re-examined on the dense source).
- Characterization only — no edge claim, no cohort action.

## Discipline / non-negotiables

- A2 basis definition, horizons, signed convention, and the `≤5s` freshness rule are **unchanged**. Only the endpoint **source** changes (defect fix).
- Missing stays missing; provenance stamped; no imputation.
- Uniform application to all episodes; adopted on the objective capture-defect finding, not to admit any specific episode.
- No admission/Guard/order impact; paper/shadow.
- The separate primary-vs-all-endpoints *eligibility* question stays a distinct, explicitly-principled pre-registration — **do not fold it into this source fix.**

## Tests (run in `.venv`)

- Endpoint availability ≈100% on a sample of SPY horizons under the dense source, where tick_log returned `UNAVAILABLE`.
- Point-in-time correctness: no endpoint uses a quote after its boundary; anchor ≤ `t0`.
- Freshness rule identical: a boundary with no fresh non-crossed quote within 5s (even dense) → `UNAVAILABLE`, not imputed.
- Provenance stamped on every label; legacy vs dense source distinguishable.
- Bias check reproduces a known synthetic correlation and a known null.
