# CMFL — Cybernetic Market Feedback Layer (DEFERRED to Phase 2)

**Status:** parked / deferred design concept. **Not scheduled for current work.** This note preserves the concept, the deferral rationale, the prerequisites that gate it, and how its pieces map onto existing Scalpr tracks — so it resumes cleanly later instead of being rebuilt from scratch. (Full 27-section spec is held by the owner; this is the summary + sequencing.)

## The concept (preserve the thesis)

Model the market as a **closed-loop feedback system**, not a static indicator canvas. Core question: if price moves, will the mechanical/participant reactions (dealer-gamma hedging, order flow, liquidity) **oppose** the move (negative feedback → mean-reversion/stabilization) or **reinforce** it (positive feedback → trend/acceleration)? Decompose into `STATE → ERROR → FEEDBACK → GAIN → DELAY → STABILITY → REGIME`.

**Why it matters:** the payoff is "never fade a high-gain positive-feedback (negative-gamma) move" — which is *exactly* the failure mode the v1 reversal funnel exposed (the setup fails because it fades continuation, treating oversold as a floor when it's fuel). The instinct is sound and aligned with the platform's own data.

## Why deferred (not now)

1. **Foundation not stable** — the collector just recovered from two dark sessions (Phase-1 regressions); base capture isn't re-verified.
2. **Base edge question unanswered** — A2 accrual ≈ 1 clean episode; the v2 verdict (*does any signal have edge?*) isn't in. CMFL is an enhancement layered on a platform proven able to measure edge; that platform isn't proven yet.
3. **Its primary inputs are unavailable/unvalidated** — the feedback engine hinges on dealer-gamma exposure, zero-gamma flip, dealer hedging direction, CVD, and depth. Those are estimated / unvalidated / in a separate repo. On reliably-available data (price/VWAP/EMA/ATR/RSI/volume), CMFL degrades toward the **regime layer already built**.
4. **Same accrual bottleneck** — validation needs labeled episodes + a producing collector; it can't be tested any sooner than everything else.
5. **Opportunity cost** — a 9-module / 16-phase build now competes with fixing the collector, accruing episodes, and running the frozen v2 test.

## Prerequisites (gates before CMFL is built)

- Collector stable and producing episodes reliably (Phase-1 resolved or superseded).
- Base edge verdict from v2 in (EDGE / NO EDGE) — CMFL builds *on top of* a platform that can measure edge.
- **Dealer-pressure inputs validated as point-in-time** (Step-0 field audit passed) — the gating data for CMFL's feedback engine.
- Regime layer validated (Stage-1a viable, conditioning signal established) — CMFL's STATE/STABILITY substrate.

## How CMFL maps onto EXISTING tracks (advance these — don't build a parallel empire)

| CMFL dimension | Existing Scalpr track that supplies it |
|---|---|
| STATE / STABILITY | **Regime Layer v0.1** (+ future v2) — extend, don't duplicate |
| FEEDBACK (gamma/flow direction) | **Dealer Pressure Advisory Study** + **CVD-Lab** |
| ERROR / deviation | already partial — VWAP/EMA/ATR deviations in the detector |
| DELAY | the freshness/latency work (tick-freshness audits, feed-staleness) |
| Decision Replay / validation | **A2 outcome labeling + Phase-4 harness** |

**CMFL = the integration of these once each is individually validated** — the unifying layer, not a from-scratch subsystem.

## The one CMFL-adjacent item that IS actionable (when there's bandwidth)

**Dealer Pressure Step-0 field audit** — is UW per-trade delta/gamma/underlying *point-in-time*, or stale/aggregated? This is the gating input for CMFL's entire feedback thesis, it's a genuine open question, and it's buildable without a working collector. **If the gamma data isn't point-in-time, CMFL's core engine can't be built honestly** — worth knowing before investing weeks in modules that depend on it.

## Discipline (recorded now, carried forward)

When CMFL is eventually built: **shadow/observational only** — no trade authorization, no gate/threshold/execution changes without explicit owner approval; no empirical thresholds or feature weights without approval (mark `DRAFT`); **look-ahead protection** (future returns only via replay, never in live features); preserve `NO_TRADE` / `UNKNOWN` / `CONFLICT` / `INSUFFICIENT_DATA`; missing ≠ fabricated; evidence chains + contradiction detection required; validated via Decision Replay + session-block matched null + walk-forward, same bar as every other signal. (The owner's spec already bakes all of this in — good.)

## Tech-stack note

Polars + DuckDB/Parquet is a reasonable low-cost local stack, but it adds new dependencies to a codebase that just had environment/venv fragility. Introduce it only when CMFL is actually scheduled — and remember the infra is the easy 5%; the real work is modeling feedback on reliable data + the year-long validation.

## Resume trigger

Revisit this note when: (a) the collector is stable and accruing, (b) the v2 edge verdict is in, and (c) the dealer-pressure Step-0 audit has confirmed point-in-time gamma. At that point CMFL becomes a real integration project rather than a premature parallel build.
