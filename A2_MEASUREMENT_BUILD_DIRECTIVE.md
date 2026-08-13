# A2 Measurement Build + Episode Sourcing — Directive (for Codex)

**Status: build directive. Paper/shadow, research-only.** No live trading, no Guard/order path, no edits to any frozen cohort/spec. Implements the open Phase-3 item (A2 measurement) and prepares the Phase-4 edge test. Governed by the frozen `A2_OUTCOME_BASIS_SPEC.md` and `REVERSAL_PHASE4_EDGE_TEST_SPEC.md` — **do not change either; build to them.**

## Part 0 — HARD GATE before any historical run: in-sample check

Before generating episodes from historical data, answer one question **in writing** and stop if the answer is wrong:

**Were the v1 reversal direction thresholds chosen a priori, or tuned on historical SPY data?**
The relevant parameters: `1.5×` 5-min ATR extension, RSI14 `35/65`, `0.15%` level proximity, the momentum-contraction and confirmation rules.

- **A priori (conventional/mechanical, not fit on SPY history)** → historical reconstruction of the frozen setup is a legitimate out-of-sample-in-time test. Proceed to Part 2 (historical) for a fast path to ≥200 episodes.
- **Any threshold was optimized/selected on SPY history** → that history is **in-sample**; a historical edge test on it is invalid. In that case, do **not** use historical reconstruction for the inferential test — use genuinely unseen data (a held-out period the thresholds never touched) or prospective accrual only.

Record the determination and the evidence (where the thresholds came from) in a short note. This gate decides whether the days-long path is even available; it is not optional.

## Part 1 — A2 measurement pipeline (the Phase-3 build)

Implement the underlying-forward-return labeler exactly per `A2_OUTCOME_BASIS_SPEC.md`. New module(s) only; no broker/order/Guard imports.

For each frozen, **non-overlapping** reversal episode at `t0`:
- Anchor = point-in-time clean SPY underlying price at `t0`; endpoints = SPY price at each horizon boundary.
- Compute `return_5m/15m/30m/60m`, `mfe`, `mae`.
- Compute the **signed** returns: `signed_return_h = return_h × setup_direction_sign` (bullish `+1`, bearish `−1`).
- **Primary label = `signed_return_60m`.** Shorter horizons are auxiliary/diagnostic only.
- **Missingness stays missing** — never impute an endpoint; mark the episode's label unavailable and exclude it from the inferential set (record the exclusion).
- **Non-overlap enforced**: one observation per episode key + cooldown (per `REVERSAL_V2_DESIGN_BRIEF.md`); correlated re-evaluations of the same window do not create new observations.
- Stamp each record `config_version/config_hash`, `episode_key`, `setup_direction_sign`, anchor/endpoint timestamps + sources, `is_calibrated_probability: false`.
- Output an append-only labeled-episode store (data → gitignored).

**Reuse** the existing `entry_policy_prototype` `return_5m/15m/30m/60m` + MFE/MAE machinery where it already does this correctly; wrap it, don't reinvent it. Add unit tests: sign convention (a correct bearish call yields positive `signed_return`), missing endpoint → unavailable (not 0), non-overlap dedup.

## Part 2 — Episode sourcing to reach the ≥200 gate

**Only if Part 0 = a priori.** Run the **frozen, unchanged** v1 reversal detector over a large body of **historical SPY 5-min RTH bars** (Alpaca historical stock bars, `data.alpaca.markets` — authenticated pull from the operator's Terminal, not an agent shell). For every detected non-overlapping episode, produce an A2 label via Part 1.

- Do **not** modify the detector, thresholds, or any gate to change the fire rate — that would be a new cohort. Run it exactly as frozen.
- Record how many distinct non-overlapping episodes result, by side (bullish/bearish) and by time period.
- If the historical path is unavailable (Part 0 failed), the only honest route is prospective accrual — flag the ~1-year timeline explicitly and do not shortcut it.

## Part 3 — Hand to the Phase-4 harness (do not run the verdict yet in this task)

Produce the labeled-episode set ready for `REVERSAL_PHASE4_EDGE_TEST_SPEC.md`:
- ≥200 non-overlapping labeled episodes (else the verdict is UNDERPOWERED — report the count, do not force it).
- Primary statistic: mean `signed_return_60m`.
- Null: session-block matched (`entry-episode-session-block-sign-null-v1` pattern), `p = (k+1)/(n+1)`.
- Walk-forward: 4 contiguous chronological folds, expanding-window, next-fold-held-out, no cross-boundary peeking for thresholding/null/verdict.
- Verdict rule (do not soften): positive mean signed 60m return **and** one-sided matched-null `p ≤ 0.05` **and** same sign on all held-out folds → EDGE; else NO EDGE; `n < 200` → UNDERPOWERED.

## Acceptance / discipline
- Part 0 determination written down before any historical run; if in-sample, historical inferential test is abandoned.
- A2 labeler matches the frozen spec exactly (signed primary, missing≠0, non-overlap); unit tests pass.
- Detector run byte-frozen; no threshold/gate change to alter fire rate.
- Labeled set is data (gitignored); code/specs committed.
- Report the episode count and the Part 0 determination back to the operator **before** the edge verdict is computed. No edge claim until the full harness clears.
