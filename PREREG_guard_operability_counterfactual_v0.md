# Pre-Registration — Guard Operability Counterfactual (v0)

**Study ID:** `guard_operability_counterfactual_v0`
**Status:** FROZEN pre-registration. Paper/shadow, **replay-only**, observational. This study **does not modify the live Guard** and holds **no** execution/admission/order authority. It replays stored quotes; it never places or manages a real or paper position. `scalp_server.py` untouched.
**Frozen at:** _(operator stamps date + git SHA on lock)_
**Relationship to the signal studies:** this is **Study 3** in the structure. It runs **beside** `prior_regime_flip_reclaim_v0` (Study A) and `intraday_continuation_v0` (Study B). Its output **never enters their signal verdicts.** "Runnability" is a joint interpretation across a signal study and this one.

---

## 1. Purpose (why this is separate)

An exit-policy failure must not be allowed to invalidate a predictive marker. Studies A/B answer *"does the marker predict the underlying?"* This study answers a **different** question: *"with the Guard continuously enabled — no pause — can Scalpr's current safety policy actually hold a candidate long enough to realize the move, or does it exit prematurely?"*

**Motivating fact (verified):** on 2026-09-03 the +205.9% paper result required the Guard **paused for the entire hold** (`guard_events_v0.jsonl`: pause 10:22:28 ET, resume 12:19:28 ET) through a **−34.9% drawdown**. The same pause-after-entry pattern appears on **all three** of that day's paper trades. That is the operator manually compensating for a Guard that, under its shipped ratchet, would very likely have exited in the early drawdown. This study measures that rigorously instead of by anecdote.

## 2. The current Guard policy under test (exact, frozen for the baseline arm)

Baseline = the **shipped** whole-position ratchet + stall (as on the trade ticket):

- Giveback ratchet: profit reaches **0% → max giveback 15%**; **15% → 6%**; **30% → 2.5%** (sells 100% of remaining in one order).
- Stall exit: **45 s** above **20%** profit; grace **60 s**; confirm reads **2**.

**Hypothesis under test (H-Guard, from the operator's observation that "the Guard closes too quickly / is blind to the macro environment"):** under the baseline policy, a materially large fraction of otherwise-favorable candidates are **stopped out in normal early post-entry noise** (the drawdown *before* a move develops), because the 0%-tier 15% giveback triggers near entry — an exit policy tuned for a mean-reverting scalp, not a drawdown-then-run convexity trade.

## 3. Replay methodology (deterministic, point-in-time, no pause)

- **Inputs:** for each candidate episode supplied by Study A or B, the stored **~5-second option bid/ask series** from the incubation path, from the episode's simulated entry to expiry/close.
- **Entry:** the episode's defined entry price/time (paper). **No pause is ever applied** — the Guard is continuously active for the whole replay. This is the counterfactual to what actually happened.
- **Fills:** conservative — exits fill at the **bid** with the frozen slippage/commission model already used by the IC study ($0.05 slippage, $0.65/leg). No mid fills.
- **Determinism:** given the same quote series the replay yields the same exit, byte-stable. No look-ahead: the exit at time *t* uses only quotes ≤ *t*.

## 4. Metrics reported per candidate (and aggregated per cohort)

- **Exit time** and **time-in-trade** (entry → guard exit).
- **Realized return** under the always-active Guard (net of costs).
- **MFE** (max favorable excursion) and **MAE** (max adverse excursion) over the full holdable window.
- **Opportunity cost** = MFE − realized return (how much of the available move the Guard left on the table).
- **Early-exit flag:** did the Guard exit while still inside the pre-move drawdown (realized < 0 and before the episode's underlying continuation confirmed)?
- **Baseline vs actual:** for episodes that were actually traded (paused), report the paused/manual outcome alongside the always-active counterfactual, so the *cost of the safety policy* is explicit.

## 5. Operability verdict (per signal cohort)

Reported **separately** from any signal verdict:

- **`GUARD-COMPATIBLE`** — over the cohort, median always-active realized return is **≥ 0** and the early-exit rate is below a pre-registered ceiling **`E_max = 40%`**. The current Guard can hold these candidates.
- **`GUARD-INCOMPATIBLE (PREMATURE-EXIT)`** — early-exit rate ≥ `E_max` **or** the always-active median is materially below the underlying-basis favorable move, i.e. the Guard is systematically exiting before the signal's move realizes. **This is a finding about the exit policy, not the marker.**
- **`UNDERPOWERED`** — fewer than the cohort's signal-study N of replayable candidates.

`E_max` and the "materially below" band are frozen on lock and cannot be loosened to force a compatibility result.

## 6. Alternative-exit-policy counterfactuals (EXPLORATORY — clearly fenced)

If the baseline is `GUARD-INCOMPATIBLE`, this study may **also** replay the same episodes under **alternative** exit policies to see whether a less myopic policy harvests more of the move. This is **exploratory / in-sample** by nature (policies chosen after seeing the baseline) and is labeled as such; **no alternative policy becomes the live Guard from this study.** Any alternative that looks promising must be **frozen as its own pre-registration** and validated on **forward** episodes before it could ever be proposed for the live Guard.

Candidate alternatives to characterize (not to adopt):
- A wider or **time-delayed early tier** (e.g., suppress the 0%-tier giveback for the first *N* seconds/until first +X%), directly testing H-Guard.
- A **macro-context-gated** giveback: widen tolerance only when a pre-registered, point-in-time macro-context condition holds (see §7). This is the disciplined seed of the operator's "read the macro environment" idea — a *conditioning gate on the exit*, not a new predictive engine, and still subject to the same freeze-and-validate rule.

## 7. Note on "macro context" (scope discipline)

The operator's insight — that the Guard is "blind to the macro environment" — is legitimate and testable, but only for macro variables that are **measurable point-in-time and captured**. In-scope candidates for future conditioning features (each requiring its own capture + pre-registration): SPY **basket breadth** (are the top-weight constituents moving with the index?), **sector-ETF / yield / VIX regime** at the timestamp. **Out-of-scope for v0 (trap-laden or unavailable at retail latency):** real-time news→price causation (post-hoc narrative), institutional fund flows (reported weeks stale). Macro context enters only as a **frozen, point-in-time gate** in a successor study — never as an after-the-fact explanation.

## 8. Prerequisites

1. Stored ~5 s option bid/ask series per candidate (incubation path — already captured).
2. Episode sets emitted by Study A and Study B loggers.
3. Deterministic Guard-replay harness implementing the exact baseline policy (§2), shared by both signal studies.
4. Replay ledger `guard_operability_counterfactual_v0.jsonl` with per-candidate metrics + provenance.

## 9. Tests (run in `.venv`)

- Determinism: identical quote series → identical exit, byte-stable.
- No look-ahead: exit at *t* uses only quotes ≤ *t*.
- Baseline fidelity: replayed baseline reproduces the shipped ratchet/stall on hand-checked fixtures (a +30% peak then −2.5% giveback exits; a 46 s stall above 20% exits).
- Pause-counterfactual: for 2026-09-03's 768C, the always-active replay is reported and its early-exit flag is computed against the −34.9% drawdown (expected: baseline exits early — this is the whole point).
- Separation: this study reads no signal-verdict field and writes none; signal studies read no field from here.
- Isolation: no code path touches the live Guard, execution, or admission; replay-only.
