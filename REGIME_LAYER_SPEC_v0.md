# Regime Layer v0 — Pre-Registered Design Spec

**Status:** pre-registered design spec. Characterization/measurement only — no new fired signal, no lock, no deployment. Paper/shadow.
**Mode:** paper / shadow only. Not operational trading.
**Scope:** a market-state (regime) classifier that *conditions* the existing frozen reversal signal. It does **not** modify the reversal detector, its thresholds, or its fire rate.

## Purpose

Single-source price action is blind to regime: the reversal detector can only infer trend-vs-chop through its own price proxies. The regime layer supplies an explicit, auditable market-state tag (trending / ranging / high-volatility) so we can measure **whether the reversal setup's edge differs by regime** — and, later, whether trading only in favorable regimes (or skipping high-volatility regimes) improves the outcome distribution.

This is the first, source-light brick of the broader multi-source model. It is built and validated on its own before any other source is added.

## What this is NOT

- **Not** a new fired entry signal. v0 only *tags* episodes; it does not create or admit trades.
- **Not** a fused score. The regime tag is a separate conditioner, kept as distinct evidence (per the §I firewall / evidence-not-fused-score discipline).
- **Not** a change to the frozen reversal detector, its thresholds, or its fire rate.
- **Not** deployed as a trade filter until measurement warrants it (Stage 2), and then only as a new frozen cohort on fresh data.

## Inputs (v0 — source-light)

- Completed 5-minute SPY RTH bars, as-of `t0` (reuse `completed_five_minute_bars`).
- Intraday ATR (reuse `wave_atr.compute_intraday_atr`).
- No new data source in v0. **Optional pre-registered extension:** VIX level/percentile as a second, orthogonal volatility input — specified now, but only added as a named variant, not silently folded in.

## Regime definition v0 (deterministic, auditable)

All features computed from **completed** bars only, as-of `t0` (no look-ahead). Two axes:

**Trend axis — Kaufman efficiency ratio (ER)** over lookback `L`:
`ER = |close[t] − close[t−L]| / Σ|close[i] − close[i−1]|` for the last `L` bars. ER→1 = clean directional move; ER→0 = chop. Bounded, deterministic, no fitting.

**Volatility axis — ATR percentile** of current ATR within a rolling window `W` of prior ATR values.

Direction = sign of `close[t] − close[t−L]`.

**States (evaluated in this priority order):**

1. `HIGH_VOL` — if ATR percentile > `atr_high_pct` (overrides trend/range; the "don't-trade or size-down" state).
2. `TREND_UP` — else if ER ≥ `er_trend` and direction > 0.
3. `TREND_DOWN` — else if ER ≥ `er_trend` and direction < 0.
4. `RANGE` — else if ER ≤ `er_range`.
5. `TRANSITIONAL` — else (`er_range` < ER < `er_trend`).
6. `UNKNOWN` — insufficient bars or any missing input (see data-state honesty).

## A-priori thresholds (the in-sample gate — do NOT skip)

Regime boundaries must be **conventional / first-principles, not fit to SPY history** — otherwise the "does edge differ by regime" analysis is in-sample and invalid, exactly like the detector-threshold trap. Pre-registered starting values (round, conventional; record provenance in writing before any measurement):

- `L = 20` bars (efficiency-ratio lookback)
- `er_trend = 0.5`, `er_range = 0.3`
- `W = 60` bars (ATR percentile window), `atr_high_pct = 0.80`

These are frozen before looking at any regime-conditioned outcome. Changing them after seeing results = a new pre-registration, not a tweak.

## Causality (critical — no look-ahead)

- Features use only bars at or before `t0`.
- If an HMM variant is ever used (see below), use **filtered (online) state estimates only** — never the smoothed / forward-backward state, which peeks at future observations and would leak look-ahead into every historical tag.

## Data-state honesty (§K)

- Missing/insufficient input → regime = `UNKNOWN`. **Never** default to `TREND` or any tradeable state.
- `UNKNOWN` episodes are recorded and excluded from regime-conditioned statistics, not silently bucketed.

## Staged plan

**Stage 0 — Pre-register (this document).** Freeze the regime definition, states, thresholds, inputs, causality, and missing-handling. Write down threshold provenance.

**Stage 1 — Measurement (non-inferential first).** Tag each A2-labeled reversal episode with its as-of-`t0` regime (an attached field on the existing episode record; no change to the detector). Once the clean prospective episode set reaches the ≥200 gate we're already accruing toward, compute mean `signed_return_60m` **by regime**, each vs its own session-block matched null.
- This is a **multiple-comparison** analysis (§J): pre-register the regime partition and the exact set of sub-hypotheses, and account for the number of regime cells tested.

**Stage 1a — Regime-distribution viability check (early, cheap, pre-registered).** Before and independent of any edge/outcome analysis, characterize how clean episodes spread across the six states. This examines **feature distribution only, not outcomes** — it is orthogonal to `signed_return_60m` — so it may be looked at **early** (well before 200 episodes) without spending any statistical power on the edge test.

- **Measure:** count and fraction of clean (non-quarantined) episodes in each state, reporting `UNKNOWN` separately from the five tradeable states.
- **Pre-registered viability criteria (a-priori; frozen before looking):**
  - No single tradeable state holds **> 70%** of classified (non-`UNKNOWN`) episodes — else there is too little contrast for conditioning to mean anything.
  - At least **2** tradeable states each hold **≥ 30** episodes — a minimum cell size to even consider a later per-regime look.
  - `UNKNOWN` fraction **< 20%** — else the classifier isn't producing usable tags often enough (a data-quality flag, not a regime finding).
- **Verdict (not an edge result):** `VIABLE` / `LOW_CONTRAST` / `INSUFFICIENT_TAGGING`. This says **nothing** about whether the signal has edge in any regime — only whether regime conditioning is worth pursuing at all. If `LOW_CONTRAST` or `INSUFFICIENT_TAGGING`, do **not** proceed to Stage 2; conditioning is dead on arrival and you've learned that in weeks, not a year.
- **Discipline:** clean prospective (v1.2) data only; criteria frozen before looking; no threshold changes after seeing the spread; `UNKNOWN` excluded from the contrast calc but its rate always reported.

**Stage 2 — Regime-conditioned signal (only if warranted).** Gated on Stage-1a = `VIABLE`. If a regime shows edge the pooled signal lacked, that is a **new hypothesis** (`reversal-in-regime-X`) requiring its **own frozen cohort validated prospectively on fresh data** — never a re-slice of the data that suggested it. Only then consider deploying regime as a trade filter (e.g., skip `HIGH_VOL`).

## The honest cost (read this before expecting a shortcut)

Conditioning does **not** reduce the data requirement — it **increases** it. Slicing 200 episodes across K regime cells leaves each cell far below 200, so a credible per-regime verdict needs *K× more* episodes, not fewer. The regime layer is a sharpening tool, not a way to get to a verdict faster. At the v1 fire rate this deepens the accrual need; that pressure is exactly what the (separate, gated) Reversal v2 fire-rate work addresses.

## Non-negotiables

- No change to the frozen reversal detector, thresholds, gates, or fire rate. Regime is an **attached advisory tag** only.
- Thresholds a-priori and frozen before measurement; provenance recorded.
- Filtered/online state only; no smoothed look-ahead.
- Missing → `UNKNOWN`, never a tradeable default.
- Regime conditioning is pre-registered with multiple-comparison accounting.
- Any regime-conditioned *signal* is a new frozen cohort on fresh prospective data.
- No fused score; regime stays separate evidence.
- Paper/shadow only; no Guard/order/execution authority in this module.

## HMM alternative (later, optional comparison)

A Gaussian-HMM regime model (fit by Baum-Welch on returns/vol features) is the standard statistical alternative. If ever built, it is a **named, pre-registered variant** compared against the deterministic v0 on the same measurement basis, using **filtered** state only. "Deterministic" is preferred here not because it is more accurate but because it is **auditable** — which is the property this research process requires. Note an HMM is a fitted model, so "no AI/no fitting" does not strictly hold for that variant; the deterministic classifier is the zero-fitting option.

## Reuse

- `completed_five_minute_bars` (bar assembly), `wave_atr.compute_intraday_atr` (ATR). Efficiency ratio and ATR-percentile are small additions. New module; no broker/order/Guard imports.
- Unit tests: ER on a pure trend → ~1 and a pure zigzag → ~0; ATR-percentile monotonicity; missing input → `UNKNOWN` (not a tradeable state); causality (tag at `t0` unchanged by future bars).
