# Pre-Registration — Market Context Layer + Context-Conditioned Guard Replay (v0)

**Study ID:** `market_context_layer_v0`
**Status:** FROZEN pre-registration. Paper/shadow, **isolated capture + replay-only**. Captures a market-context ledger and replays exit policies offline. It **does not wire to the live Guard, execution, or admission**, and does not modify `scalp_server.py`. Hard catastrophic-loss and data-integrity protections in the live system remain **unconditional and untouched** by anything here.
**Frozen at:** _(operator stamps date + git SHA on lock)_
**Position in the structure:** extends **Study 3** (`guard_operability_counterfactual_v0`). Its output never enters the **signal** verdicts of Study A / Study B.

---

## 1. Boundary principle (the whole point)

Scalpr **cannot reliably know *why* SPY is moving in real time.** It can measure whether the broader market is **confirming** the move and whether conditions justify giving a position more room. This layer produces a **confirmation state**, never a causal claim and never (initially) a calibrated probability. It may only **select among pre-frozen exit profiles** — it may never disable the Guard, and no discretionary pause switch is part of the model.

**Three separated questions (never conflated):**
1. Was the option entry directionally justified? → Studies A / B (signal).
2. Is the market continuing to confirm the thesis? → **this layer** (context).
3. What loss- and profit-protection policy is permissible? → Guard profile selection (§4), gated by (2), bounded by unconditional hard protection.

## 2. The honest risk this design must beat

Most context signals (SPY > VWAP, breadth advancing, QQQ/ES up) are **contemporaneous restatements of "SPY is going up right now."** Conditioning the exit on "the market is confirming" therefore risks collapsing into nothing more than **"hold winners longer"** — a trailing-stop-*width* question dressed up as market intelligence. So the confirmation layer must **earn its complexity**:

- The context-conditioned Guard must **beat a naive wider-trailing-stop control** (§5) out-of-sample. If a plain wider stop captures the same gain, the context machinery is unjustified overfitting and is rejected.
- A confirmation score is only useful if the confirmed condition **persists into the forward window** — context-at-t must predict continuation-*after*-t, not merely describe-at-t. That is the property under test, not an assumption.

## 3. Part 1 — Market Context Shadow Ledger (capture; begins NOW, forward-only)

Sampled every **30–60 s** during RTH, written to an isolated `market_context_shadow_v0.jsonl`. Point-in-time only; every field freshness-stamped. This capture can start immediately and independently, because point-in-time context **cannot be backfilled** — it only exists going forward.

**Signal families (capture what is actually feedable; see §7 availability):**
- **Breadth:** % constituents advancing, up/down volume, advance–decline trend.
- **Leadership:** contribution + momentum of SPY's **largest weights** (cap-weighted, not equal-weight counts).
- **Cross-asset:** ES futures, QQQ/IWM, Treasury yields, VIX, dollar, relevant sectors.
- **Market structure:** SPY vs VWAP, VWAP slope, opening-range break, higher-lows, realized vol, spread quality.
- **Options structure:** gamma regime, gamma flip, walls, 0DTE positioning, and whether price is moving **toward vs through** those levels (FlashAlpha).
- **Catalysts:** timestamped earnings, econ releases, Fed remarks, corporate news, large disclosed transactions (§6).
- **Persistence:** breadth + leadership remaining aligned across **several consecutive** observations, not a single flash.

**Emitted state (example — a confirmation score, NOT a probability):**
```
state: TREND_CONFIRMED
direction: bullish
breadth_confirmation: 0.82
weighted_leadership: 0.76
cross_asset_confirmation: 0.68
structure_confirmation: 0.91
event_confidence: 0.40
persistence_reads: 4
data_freshness: CLEAN
```
Each sub-score's formula is **frozen on lock**. `data_freshness` ∈ {CLEAN, DEGRADED, STALE}; any stale/missing input degrades the state and (per §4) can only ever **reduce** permitted risk.

## 4. Part 2 — Frozen context→exit-profile mapping (pre-registered, deliberately simple)

The mapping from context state to Guard profile is **frozen before any session counts** and kept small (few profiles, few gates) to limit researcher degrees of freedom:

- **Unconditional floor:** hard catastrophic-loss and data-integrity exits fire regardless of context. Context can never widen past this floor.
- **Strong, persistent confirmation** (state = TREND_CONFIRMED, persistence ≥ frozen `P_min`, freshness CLEAN) → a **slower, pre-frozen trailing profile** (more room).
- **Mixed / single-flash / neutral** → the **standard shipped Guard** (baseline).
- **Negative / deteriorating confirmation** → **tightens** protection vs baseline.
- **Missing or stale context** → **never** grants extra room; defaults to standard or tighter.
- **No discretionary pause** exists in the model.

Every profile referenced here is itself pre-frozen (its exact ratchet/stall parameters committed on lock). Context **selects among** them; it never invents one live.

## 5. Part 3 — Counterfactual Guard replay (offline, deterministic, no look-ahead)

Replay every Study A/B candidate under **five** policies on the stored 5 s option quotes, joined to the context ledger by timestamp (context read must be ≤ decision time):

1. **Existing Guard** (baseline shipped ratchet/stall).
2. **Context-conditioned Guard** (§4 mapping).
3. **Naive wider trailing stop** — *the control I am adding* — a fixed wider trailing profile with **no context input**. This is the honesty check: (2) must beat (3), or context adds nothing.
4. **Fixed-time exit** (e.g., hold to a frozen clock time).
5. **Unmanaged hold** to expiry/close (upper-bound reference).

**Per-candidate + per-cohort metrics:** exit time, time-in-trade, realized return (net costs, bid fills), MFE, MAE, opportunity cost (MFE − realized), early-exit flag.

**Reading the result (reported separately from signal verdicts):** the context-conditioned Guard is judged **superior only if** it beats **both** the existing Guard **and** the naive wider stop on out-of-sample, walk-forward cohorts — otherwise context is rejected as unjustified complexity. As with all studies: matched null and 4-fold chronological walk-forward mandatory; today is **hypothesis-generating / in-sample**; the context rules must be **frozen before subsequent sessions count**.

## 6. Catalysts — record, don't rationalize

A Snowflake headline, an ETF rebalance, or fund buying may **coincide** with SPY's move without causing it. Rule: **record timestamped catalysts** in the ledger and **measure their incremental predictive value against breadth and price structure** — a catalyst feature is admitted to the mapping **only if** it improves out-of-sample outcome **beyond** what breadth + structure already provide. Scalpr never manufactures a causal explanation after the move, and never widens the Guard on a headline alone.

## 7. Data-availability honesty (do not spec a capture we cannot feed)

Some families are cheap to capture from the current stack; others need sources we do **not** have. v0 captures only what is genuinely feedable point-in-time; the rest are **deferred** exactly like executed-volume was in Study B — captured only when a real source exists, and folded in only via a successor study.

- **Feedable now** (Alpaca SIP / FlashAlpha we already use): SPY structure (VWAP proxy, ORB, realized vol, spread), **ETF cross-asset** (QQQ, IWM, sector ETFs), **options structure** (FlashAlpha gamma/flip/walls/0DTE). VIX **index** level if the feed carries it.
- **Needs a source / verify before relying on** (likely **not** in the current equity SIP feed): **full 500-constituent breadth** (requires streaming all names), **ES futures**, **live Treasury yields**, **dollar index**, real-time **news/catalyst** wire. These are **deferred**; a v0 "breadth" proxy may use a **capturable subset** (e.g., a fixed basket of the largest weights we can stream) and must be **labeled a proxy**, never presented as true 500-name breadth.

The operator confirms which sources are actually available before those fields leave `DEFERRED`.

## 8. Sequencing (my recommendation, flagged for decision)

- **Start Part 1 capture now.** It is cheap, isolated, forward-only, and non-inferential. Delay = permanently lost context data.
- **Freeze Part 2 mapping** on lock, but **do not wire it to anything.**
- **Run Part 3 replay later**, once (a) context data has accrued and (b) a signal study has shown something worth holding. Building an elaborate context-conditioned exit before any entry marker is validated is a roof without a foundation — the capture is what must not wait.

## 9. Prerequisites

1. Isolated context sampler (30–60 s) writing `market_context_shadow_v0.jsonl`, every field freshness-stamped, feedable sources only (§7).
2. Frozen sub-score formulas + the context→profile mapping (§4), SHA-committed.
3. Deterministic multi-policy replay harness (extends the Study 3 Guard-replay harness) with the five policies incl. the naive-wider-stop control.
4. Operator confirmation of available data sources before any `DEFERRED` field is enabled.

## 10. Tests (run in `.venv`)

- Point-in-time: every context read joined to a replay decision is ≤ the decision timestamp; no forward context leaks into an exit.
- Freshness asymmetry: a STALE/missing context state can only select **standard or tighter** — never the slower/wider profile. (Adversarial test: stale-but-bullish input must not widen.)
- Control integrity: the naive-wider-stop policy reads **no** context field.
- Rejection logic: on a fixture where the naive wider stop matches the context-conditioned Guard, the report marks context **not justified**.
- Catalyst increment: a catalyst feature that adds nothing beyond breadth+structure on a fixture is **not** admitted.
- Isolation: no path touches the live Guard/execution/admission; capture and replay only. In-sample exclusion of pre-lock sessions.
- Proxy labeling: any subset-breadth field is stamped `proxy`, never `sp500_breadth`.
