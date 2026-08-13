# Scalpr — Design Review Brief for a Third Opinion

**Purpose:** This document captures a research/design conversation between the operator (Chad) and an advising assistant about the *direction* of the Scalpr project. It is written so that a second, independent LLM (or a human reviewer) can read it cold and give a genuine third opinion. **Please challenge the positions below — they are the assistant's arguments, not settled truth. Disagreement is the point.**

**Nothing here is live.** Scalpr is a local, single-user **paper/shadow** options-trading *research* platform. No module discussed has order, broker, or execution authority. Nothing has been locked or committed. This is exploratory.

---

## How to review this document

We want a third opinion specifically on **direction** — is the discipline paradigm sound, or are we over-constraining ourselves? Are the cautions correct, or too conservative? At the end there is a list of **explicit review questions**. Please answer those directly, agree or disagree with reasons, and flag anything you think is wrong, missing, or overcautious.

---

## 1. What Scalpr is (context for the reviewer)

Scalpr is a local research harness for studying short-dated SPY options setups. Key facts:

- **Paper/shadow only.** No real money, no live orders. The research subsystem imports no broker/order/Guard code and has no execution authority.
- **Automated scope is deliberately narrow:** SPY options, 0–2 DTE. There is a separate, wider *manual* paper builder (0–60 DTE) that is isolated from the automated validator by a byte-frozen scope check.
- **Design philosophy — "evidence, not probability."** The system deliberately does **not** emit a calibrated confidence or a single fused score. Records carry `is_calibrated_probability: false`. "Missing ≠ neutral." No look-ahead. Append-only, hash-stamped, immutable records.

### The current research object: "Entry Intelligence v1"

A frozen, prospective, executable-bid measurement of whether a mechanically-defined SPY reversal setup has positive net expectancy versus abstaining. Two mirrored cohorts: `low_reversal_v1` (CALL) and `high_reversal_v1` (PUT).

- **Direction is fully mechanical:** 1.5× 5-min ATR extension, level proximity (0.15%), momentum contraction / Wilder RSI14 turn (35/65), and a completed-bar higher-low/reclaim confirmation. All four groups required or it's `NO_TRADE`.
- **Three separated evidence axes, never fused:** (1) *Direction* — did the mechanical reversal occur? (2) *Quality* — how much pre-registered confluence is present? (3) *Executability* — is there a fresh, tradeable option contract at a two-sided quote?
- **Executable-bid outcome geometry:** simulated entry at the fresh ask; stop = ask×0.80; target = 1.5R; measured only on fresh executable bids. Frozen cost model ($0 commission, $0.12 round-trip regulatory, 1-tick slippage).
- **Validation gate before any "edge" claim:** ≥200 non-overlapping episodes + frozen cost model + session-block permutation null + untouched walk-forward window. No parameter changes after the prospective pre-open lock.
- **Current status:** both cohorts are `DRAFT_NOT_LOCKED`, `operator_confirmed: false`. A dry run is planned to prove the data plumbing captures clean, trackable bids. **No edge has been demonstrated. The hypothesis is untested.**

### Three separate subsystems (often conflated)

1. **Entry Intelligence** — research pipeline measuring whether the reversal pattern has edge. Simulated, no real position.
2. **Guard ratchet** — protective exit engine that manages the operator's *manual* paper trades (ratchets a profit floor up, exits on giveback). Not connected to Entry Intelligence.
3. **Wave Riding / climb ladder** — a separate shadow study on *scaling into* a winner. Notably, its first cohort produced **zero additions across 33 simulations** — scaling is unproven and dormant.

These do not currently talk to each other.

---

## 2. The conceptual thread of this conversation

The operator was building a "gestalt" mental model and testing it against the assistant. The exchanges, in order:

### 2a. "When I run a pre-trade check, does it tell me this is a good trade?"

**Assistant's position:** No — not today. A pre-trade check reports (a) whether the frozen mechanical pattern is present and (b) whether there's a clean, tradeable contract. That is an *eligibility/consistency* read on an **unvalidated** pattern — not a profitability verdict. Two clarifications: the check applies **fixed rules to current data** (it does not synthesize all accumulated history into a per-trade quality score), and even those rules have **zero validated edge yet**. A green check today means "clean, on-pattern candidate," not "likely winner." It becomes a genuine quality signal only after a cohort locks, banks ≥200 episodes, and clears the null + walk-forward.

### 2b. The layered model: Entry → Stability → Growth

The operator proposed a global view: (1) Entry, (2) Stability, (3) Growth, and asked whether the collected data will "over time detect if the technicals align to make a good trade, then a ladder determines how long to hold and scale."

**Assistant's position:** The learning half is roughly right (data validates whether the pattern has edge, over many sessions). The "ladder holds and scales" half fuses **three different machines**: Entry Intelligence's fixed target/stop is a *research yardstick* (simulated, not adaptive); the Guard ratchet is the real hold-and-lock-profit engine but operates on *manual* trades; and scaling is a separate, currently-empty study. The integrated "detect → hold → scale" pipeline the operator pictured **does not exist** and would each need to be built and independently validated.

### 2c. A macro / market-environment layer (the "4th layer")

The operator asked whether we should scan the broad market (indices, quarterly cyclicals) to inform the micro decision.

**Assistant's position:** No macro layer today, and you don't need one to ship the micro work — but the architecture lets you answer the macro question empirically *later*. Cautions raised: (i) **timescale mismatch** — quarterly cyclicals flow over months; a 0–2 DTE scalp resolves in ~60 minutes; (ii) **overfitting** — every conditioning variable is a knob that multiplies researcher degrees of freedom; (iii) the disciplined path is to **tag each recorded episode with the macro state at decision time** (free, even retroactively) and let the accrued data show whether a regime split has signal — then freeze it as a conditioned cohort and prove it prospectively. **Volatility regime** is the most defensible candidate and directly informs hold-time. Note SPY and SPX are the same underlying (ETF vs index).

### 2d. Adding multiple correlated instruments (the crux)

The operator proposed feeding **QQQ, VIX, DIA, VXX, VIXY, UVXY, SVOL, IWM** into the model to "qualify and detect a good path and make a recommendation instead of a yes/no," and floated a model that **actively justifies and defends its own decisions**, **adjusts its foundation to volatility**, and could **rotate the position to a stronger instrument** (e.g., "we expected SPY but DIA is advancing, move there").

**Assistant's position (the core of the review):**

1. **Correlation collapse.** Those eight instruments are ~two independent factors, not eight signals. SPY/QQQ/DIA/IWM are equity-index ETFs correlated ~0.85–0.97 intraday. VIX/VXX/VIXY/UVXY/SVOL are all the *same volatility factor* (VIX index; VXX/VIXY/UVXY are long-VIX-futures products at different leverage; SVOL is the inverse). Stacking them is one signal counted many times.

2. **Redundant correlated predictors cloud, not enhance.** They add little independent information but a lot of fitting surface — inflating in-sample signal while increasing out-of-sample fragility. What genuinely carries information is the **volatility factor as a conditioning axis** and the **divergence between indices** (e.g., SPY reversing while QQQ refuses to confirm) — each is *one derived feature to test*, not a raw firehose.

3. **Self-justification is a trap.** A model that argues for its own position produces fluent *rationalization*, not validation — a language model can justify any decision after the fact. The honest form of "self-questioning" is the counterfactual/falsification record already in the design: record what the setup would have done, and let accumulated data adjudicate.

4. **Real-time self-adaptation destroys measurability.** A model that changes its own rules on the fly can never be validated (no frozen hypothesis to test). Adaptation belongs *between* cohorts (freeze → measure → new frozen cohort). The only honest in-flight version is **conditioning on a pre-registered state variable** (e.g., vol regime), where the response function was frozen *ahead of* the data.

5. **Cross-instrument rotation is a different hypothesis.** "Move to DIA because it's advancing" is relative-strength rotation — a separate strategy on different instruments needing its own frozen validation. You don't get it by feeding data into the SPY reversal model.

6. **Affirmed the operator's framing:** "We're not taming chaos; we're finding paths that work most of the time by percentages." Correct — but "works most of the time by percentages" is a claim you can only *earn* with out-of-sample proof, and every unproven correlated input makes the percentage you think you have less trustworthy, not more.

---

## 3. The design principles at stake (stated plainly for challenge)

- **P1 — Freeze then test.** A hypothesis is pre-registered, frozen, and tested prospectively out-of-sample before any edge claim. Parameters cannot change after lock; a change means a new cohort id.
- **P2 — Evidence, not probability.** No fused score, no calibrated confidence until validated. Axes are separated and ordinal.
- **P3 — Missing ≠ neutral.** Unavailable/stale data is recorded as such, never imputed or treated as a passing/neutral value.
- **P4 — Add knobs only after the base earns its place.** Extra inputs/conditions (macro, multi-instrument, scaling) enter as *individually tested* conditions after the base is validated — never as a naive enrichment.
- **P5 — Counterfactual falsification over narrative justification.** The system records what would have happened and lets data adjudicate, rather than generating explanations that defend decisions.
- **P6 — Adaptation between cohorts, conditioning within them.** No on-the-fly rule changes; only pre-registered state-conditioned responses.

> **Update:** see `SCALPR_V2_FLOW_AMENDMENTS.md` Rev 5 (§I–§K) for the three governance rules adopted here: non-qualifying-signal firewall, per-family sample budget, and hardened data-state.

---

## 4. Explicit questions for the third opinion

Please answer these directly, and disagree where warranted:

1. **Is the frozen-cohort + prospective-validation paradigm the right discipline for this problem, or is it too rigid to ever capture a real, regime-dependent edge in options microstructure?**
2. **Is the "correlation collapse" argument sound?** Do QQQ/VIX/DIA/VXX/VIXY/UVXY/SVOL/IWM really reduce to ~2 factors for this purpose, and is the claim that correlated inputs "cloud not enhance" correct? Are there specific derived features (index divergence, vol-regime, term-structure of VIX futures, breadth) worth testing — and how would you guard them against multiple-comparisons overfitting?
3. **Is rejecting real-time self-justification and self-adaptation correct?** Or is there a disciplined adaptive/online-learning approach that preserves measurability and is worth considering?
4. **Is ≥200 episodes a defensible sample** given options noise, cost, and the number of future conditions we might test? How should we budget for multiple comparisons as conditions accumulate?
5. **Is the whole endeavor sound given 0–2 DTE options microstructure** (spreads, slippage, gamma/theta, the executable-bid path)? What failure modes are we underweighting?
6. **What are we missing?** Anything in the direction, the discipline, or the roadmap (micro → macro → multi-instrument → scaling) that a fresh reviewer would flag.

---

## 5. One-paragraph summary for a busy reviewer

Scalpr is a paper-only research harness testing whether a fully mechanical SPY 0–2 DTE reversal setup has positive net expectancy, measured prospectively on executable option bids, with a hard validation gate (≥200 non-overlapping episodes + permutation null + walk-forward) before any edge is claimed. The operator wants to grow this into a layered system (entry → hold → scale) and enrich it with a market-environment layer and multiple correlated instruments, plus a model that justifies and adapts its own decisions. The assistant argued for strict discipline: the base edge is unproven, correlated instruments mostly add overfitting surface rather than information (they collapse to ~2 factors), self-justification is rationalization not validation, real-time self-adaptation destroys measurability, and any enrichment (macro regime, index divergence, scaling, rotation) must enter as an individually pre-registered, prospectively-validated condition — not a naive data firehose. **We are asking for a third opinion on whether that direction and that discipline are right, too rigid, or missing something.**
