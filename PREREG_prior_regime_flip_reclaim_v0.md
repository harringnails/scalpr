# Pre-Registration — Prior-Regime Flip-Reclaim Study (v0)

**Study ID:** `prior_regime_flip_reclaim_v0`
**Status:** FROZEN pre-registration. Paper/shadow, observational, **NON-INFERENTIAL** until the sample target below is met and the verdict rule fires. No admission, no Guard, no order authority. Nothing here authorizes a trade.
**FROZEN / operator-approved:** 2026-09-04. This file is SHA-256 stamped into every episode record and version-locked by its freeze commit.
**Prospective accrual begins:** the **next full RTH session after the 2026-09-04 freeze commit.** Every session on or before 2026-09-04 is **in-sample / exploratory** and excluded from the inferential count.
**Mutation rule:** no parameter may change after this commit. Any change requires a new study ID and resets N to zero.

---

## 1. Motivation (honest, outcome-independent)

On 2026-09-03 a 0DTE SPY 768C returned +205% (paper) on a **0.50%** underlying move (768.79 → 772.66, verified in `tick_log.csv`). Post-hoc, the prior session had closed in a **negative-gamma** regime (spot 765.62 < gamma flip 767.65), and SPY reclaimed that flip premarket and trended. That is a *story fit to one winner*, told with the answer already visible. It is not evidence. This pre-registration exists to convert that story into a falsifiable question and test it on data frozen **before** the outcome is known.

**Explicit caveat carried into the design:** the intraday regime on 2026-09-03 was later classified **positive gamma** by FlashAlpha (full-chain GEX ≈ +$856M at 10:21 ET). Therefore "dealers were short gamma and chased the move" is **not** the mechanism under test. The prior-close negative-gamma read is treated only as a **prior/context filter**, never as an assumed live driver.

## 2. The frozen hypothesis (H1)

> When SPY closes session *D−1* in a **negative-gamma regime** (prior-close spot < prior-close gamma flip), **and** during session *D* SPY **reclaims and holds above** the *D−1* gamma-flip level per the acceptance rule in §4, **then** the *D* session continues **directionally in the reclaim direction** (here: upside) by more than a session-time-and-volatility-matched control, measured on the underlying forward-return basis in §5.

**Direction rule (resolves the direction ambiguity that gamma alone cannot):** the tested direction is the **sign of the flip reclaim** — a reclaim from below → test **long/upside** continuation; a rejection/loss from above → a *separate, symmetric* short hypothesis that must be pre-registered on its own before any short episode counts. v0 covers the **upside-reclaim** arm only.

**Null (H0):** conditional on the same session-time block and realized-vol bucket, flip-reclaim episodes show **no** forward-return advantage over matched non-episode windows.

## 3. Premarket vs RTH — two distinct sub-hypotheses (do NOT merge)

2026-09-03 reclaimed the flip at **08:57 ET — premarket** — and opened already above it. A rule that says "first RTH hour reclaims the flip" would **not** describe that day. The two cases are different phenomena and are counted in **separate cohorts**:

- **H1a — Premarket reclaim + RTH acceptance:** flip is reclaimed before 09:30 ET; test is whether RTH **holds** acceptance (§4) and continues.
- **H1b — Intraday RTH cross:** SPY is **below** the flip at 09:30 ET and crosses from below to above during RTH; test continuation from the cross.

Each cohort accrues its own N, its own null, its own verdict. No pooling.

## 4. Episode definition (point-in-time, no look-ahead)

- **Regime gate (D−1):** prior-close FlashAlpha read with `gamma_regime == negative` AND `spot < gamma_flip`, using the **last fresh** prior-session read (freshness per §7). Level `F = D−1 gamma_flip`, frozen at *D* open; it does **not** update intraday.
- **Reclaim:** first timestamp where SPY two-sided mid ≥ `F` (H1a: any time; H1b: during RTH from a below-open).
- **Acceptance (hold):** SPY mid remains ≥ `F − A` for a continuous **acceptance window** `W` after reclaim, with no more than `G` seconds cumulative back-below. **Frozen parameters:** `A = 0.20` pts, `W = 900 s`, `G = 120 s`.
- **Anchor t0:** the timestamp acceptance completes. Forward returns are measured from t0.
- **One episode per session per cohort.** Non-overlap is automatic (one anchor/day). Sessions with no fresh prior-close regime read, or no fresh SPY quote at t0, are **excluded and logged** — never imputed.

## 5. Outcome basis (identical to the platform's A2 dense basis)

- **Basis:** SPY **underlying forward return** from t0, dense-labeled from **Alpaca historical SIP stock quotes**, two-sided mid = (bid+ask)/2, `≤ 5 s` freshness, non-crossed. Horizons **5 / 15 / 30 / 60 min**, all required; a missing horizon → episode `A2-UNAVAILABLE`, excluded, logged.
- **Signed convention:** upside-reclaim arm scores **+** for favorable (up) continuation.
- **Also recorded (descriptive, non-scoring):** MFE, MAE, and time-to-MFE on the underlying. Option-level P&L is **not** the inferential basis (0DTE option returns are leverage, not edge; the underlying basis removes contract selection and theta as confounds).

## 6. Guard operability — measured SEPARATELY, NOT mixed into the signal verdict

The 2026-09-03 win occurred with the Guard **paused for the entire hold** through a −34.9% drawdown, so operability is a real question. But an exit-policy failure must **not** be allowed to invalidate a genuine predictive marker — that conflates two different questions (does the marker predict? vs. can the current exit policy harvest it?). Therefore:

- **This study's verdict (§8) is a pure signal test on the underlying basis.** It contains no Guard result of any kind.
- Guard operability is measured by the separate **Guard Counterfactual study** (`PREREG_guard_operability_counterfactual_v0.md`), which replays every candidate with the Guard **continuously enabled** and reports exit time, realized return, MFE, MAE, and opportunity cost.
- **Runnability is a joint interpretation, decided only after both studies complete:** a marker is a *runnable Scalpr strategy* only if this signal study returns `EDGE` **and** the Guard study returns operationally-compatible. A signal `EDGE` with an incompatible Guard is a **signal to fix the exit policy — not to discard the marker.**

## 7. Freshness, provenance, exclusions

- Every FlashAlpha and SPY read stamped with provider timestamp + age; stale (> 5 s at the decision boundary) → excluded, logged, never imputed.
- **Historical/Databento reconstruction is EXPLORATORY / NON-INFERENTIAL** and cannot contribute to the N. Only forward, live-captured sessions count.
- **2026-09-03 and all pre-lock sessions are in-sample** and excluded from the inferential count. They may be reported separately as the generating example.

## 8. Sample, null, walk-forward, verdict

- **Target N:** ≥ **150** non-overlapping qualifying episodes **per cohort** (H1a, H1b separately). Below target → **`UNDERPOWERED`**; no verdict, no exceptions.
- **Matched null:** for each episode, draw control windows from the **same session-time block** and realized-vol bucket without the reclaim condition; significance by the frozen permutation estimator **p = (k + 1) / (n + 1)**.
- **Walk-forward:** 4-fold **chronological** (no shuffling); the direction/edge must hold **out-of-fold**, not just pooled.
- **Verdict (pre-registered, no threshold loosening ever):**
  - **`EDGE`** — pooled effect positive at p ≤ 0.01, **consistent sign in ≥ 3 of 4 chronological folds** (signal only; Guard operability is reported separately per §6 and never enters this verdict).
  - **`NO EDGE`** — target N met, criteria not satisfied.
  - **`UNDERPOWERED`** — N below target.
  - _Runnability_ (`EDGE` **and** Guard-compatible) is a joint interpretation across this study and the Guard Counterfactual study — it is **not** a verdict category here.

## 9. Freeze mechanics (non-negotiable)

- On lock, this file is committed and its SHA recorded in the study ledger header. **No parameter** (`F` source, `A`, `W`, `G`, horizons, N, p-threshold, fold rule, verdict thresholds) may change post-lock. Any change = a **new** study ID starting a fresh N from zero. Loosening a threshold to reach a verdict voids the study.
- Isolated: reads ledgers only; **no** wiring to execution, admission, collector, server, A2, or dense pipelines. `scalp_server.py` untouched.

## 10. Prerequisites before accrual can score

1. **Prior-close regime capture** persisted per session (FlashAlpha `gamma_regime`, `gamma_flip`, `spot`), freshness-stamped.
2. **Point-in-time SPY dense labeling** at arbitrary t0 (already available via the dense A2 source).
3. **Always-active-Guard replay harness** over stored 5 s option quotes (§6).
4. Episode-logger writing `prior_regime_flip_reclaim_v0.jsonl` with full provenance + exclusion reasons.

## 11. Tests (run in `.venv`)

- Point-in-time correctness: no reclaim/acceptance/outcome read uses data after its boundary; `F` frozen at open.
- H1a vs H1b routing: premarket-reclaim vs RTH-cross episodes land in the correct cohort; a below-open→RTH-cross fixture routes to H1b, an already-above-open fixture to H1a.
- Exclusion integrity: stale regime read, missing horizon, no fresh t0 quote → excluded + logged, never imputed.
- Null estimator reproduces a known synthetic effect and a known null.
- Signal/Guard separation: the signal verdict is computed with **no reference to any Guard field**; a synthetic episode that trends on the underlying but would stop out under the Guard still returns `EDGE` here (its operability is recorded only in the separate Guard study).
- In-sample exclusion: 2026-09-03 is not counted toward N.
