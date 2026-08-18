# Regime Layer v0.1 — Warmup Scope Correction (pre-registered revision)

**Status:** pre-registered revision, v0 → v0.1. Advisory-only, Stage 0/1. Paper/shadow. Amends `REGIME_LAYER_SPEC_v0.md`. **No threshold change.**

## Problem (surfaced by live data, 2026-08-14)

The one admitted episode (CALL, 11:55 ET) returned `regime_tag: UNAVAILABLE`. Root cause: the classifier requires `ATR_PERIOD(14) + ATR_WINDOW(60) = 74` completed 5-minute bars, ≈ 6.2 hours — nearly the entire 6.5-hour RTH session. As implemented, the 60-bar ATR-percentile window fills from **same-session bars only**, so regime is `UNAVAILABLE` until ~bar 74 (≈ 15:40 ET) and every earlier intraday episode is `UNKNOWN`. Since reversal episodes fire throughout the session, **regime conditioning is effectively dead for intraday signals** — the layer passed every unit test yet is unavailable in practice.

## Fix — window **scope** correction (not a parameter tune)

Draw the 60 prior ATR observations from a **rolling cross-session tail**: the most recent 60 completed 5-minute intraday-ATR observations, **carried across session boundaries** rather than reset at each open. The percentile then has a valid reference from the first bar of a new session (using the prior session's tail).

This is the intended meaning of "60 prior ATR observations" — v0 under-specified it as same-session-only. It is a **scope correction, so threshold provenance stays a-priori.**

## Unchanged (a-priori, frozen)

`ER_LOOKBACK=20`, `ER_TREND=0.5`, `ER_RANGE=0.3`, `ATR_PERIOD=14`, `ATR_WINDOW=60`, `ATR_HIGH_PERCENTILE=0.80`, the six states, the priority order, and the causality rule are all **unchanged**. Only the *source* of the ATR-percentile history changes.

## Residual limitation (accepted, documented — not hidden)

The efficiency ratio stays **same-session** (20 bars, ≈ available from ~bar 21 / ~11:10 ET). Carrying ER across the overnight gap would blend the prior close into the new open and pollute the intraday trend read, so ER is deliberately not cross-session. Net effect of v0.1: regime becomes available from **~bar 21** of each session (ER-bound) instead of **~bar 74** (ATR-window-bound). Episodes in roughly the first ~100 minutes still tag `UNKNOWN` — this is an accepted, stated limitation, not a silent gap. (Today's 11:55 ET episode is past bar 21, so under v0.1 it would have received a real tag.)

## Causality (preserved)

- Only completed bars at or before `t0` are used; prior-session bars are historical, so there is **no look-ahead**.
- If total available ATR history `< ATR_PERIOD + ATR_WINDOW`, or ER is not yet available, the state stays `UNKNOWN`/`UNAVAILABLE`. Missing stays missing.
- If an HMM variant is ever used, filtered (online) state only — unchanged.

## Re-tagging

Regime is advisory and **no inferential regime-conditioned data has accrued** (every live tag so far is `UNAVAILABLE`). So re-classifying prior episodes under v0.1 for characterization is fine — it contaminates no inferential set. Going forward, new episodes receive proper tags.

## Version / provenance

Bump `REGIME_VERSION` to `deterministic-regime-layer-v0.1`; update `THRESHOLD_PROVENANCE` to note the ATR-percentile-window scope correction (same conventional thresholds, now sourced from a rolling cross-session tail) with its a-priori justification. Still Stage 0/1, advisory-only, no admission/gate/sizing authority.

## Implementation notes

- The regime module must receive (or load) a rolling tail of completed 5-minute bars spanning enough prior sessions to fill the 60-bar ATR-percentile window from session open (≈ the prior 1–2 RTH sessions).
- Reuse `wave_atr.compute_intraday_atr` per session; the percentile window is the last 60 completed-bar intraday-ATR observations across the carried tail.
- Tests: (a) at ~bar 21 of a new session **with** a prior-session ATR tail present, state is non-`UNKNOWN` where v0 would be `UNKNOWN` until bar 74; (b) ATR-percentile computed from the carried tail matches a direct computation; (c) still `UNKNOWN` when history is insufficient; (d) causality — tag at `t0` unchanged by future bars; (e) thresholds unchanged. Run in `.venv`.
