# Reversal v2 Design Spec (tightened)

**Status:** pre-registered design spec — tightened from the v2 design brief. No implementation decision yet.
**Mode:** paper / shadow only. Not operational trading.
**Scope:** reversal study redesign. New frozen cohort; not a tweak or reuse of the current one.

Supersedes the initial brief. Four tightenings from review are folded in: (1) outcome-basis viability is now the **gating first stage**, not a co-equal question; (2) the redesign process itself carries multiple-comparisons discipline; (3) the widen-vs-edge tradeoff is a first-class risk; (4) the review gate carries quantitative targets so it can actually reject a bad design.

## Purpose

The current reversal study is doubly constrained by two independent, evidenced blockers:

1. **Rarity** — the direction definition is too restrictive; the binding groups are `momentum_slowing` and `causal_confirmation`. ~4 distinct fires in 6 sessions.
2. **Labelability** — single-option executable-bid tracking is not reliably dense enough to clear the 80% coverage bar. Observed strike freshness is bimodal (two of four strikes ~3% FRESH, two ~80%); which strike the selector lands on is close to a coin flip w.r.t. quote density.

v2 exists to test a salvage design with those constraints explicit. It is **not** a relaxed reuse of the current setup.

## Gating order (this is the key structural change)

Do these **in sequence**. Do not design later stages until the earlier gate passes.

- **Stage A — Outcome-basis viability (gating).** Resolve whether *any* contract-selection policy can reliably clear the coverage bar. If no, the direction and selection work is moot and v2 needs a different outcome basis. **This is decided first.**
- **Stage B — Direction definition (rarity).** Only if Stage A passes.
- **Stage C — Contract-selection policy (labelability).** Co-designed with B once A is resolved.

## Non-Negotiables

- No live trading. No Guard, broker, or order-path changes. No editing of the current frozen cohorts.
- No loosening of the `<= 5s` freshness definition to force coverage.
- No reuse of the current cohort id, hash, or acceptance basis.
- Any direction change registers as a **new frozen cohort** before evidence collection starts.
- **Meta-discipline (new):** v2 is designed *after* looking at v1's data, so:
  - v2 must be validated on **fresh prospective data it was not designed on** — v1's captured records are for design/characterization only, never for the inferential test.
  - The candidate design variants are **pre-registered up front and capped** (small, named set). A failed v2 → v3 is a **new cohort on new data**, not a re-tune on v2's data. No "loosen, watch the fire rate, loosen again" laundered through new cohort ids.
  - Record how many design variants were considered (for honest multiple-comparison accounting).

## Stage A — Outcome-basis viability gate (the promoted Question 3)

**Question:** can single-option executable-bid tracking clear the coverage bar reliably enough to be a valid outcome basis on SPY 0–2 DTE strikes — or must v2 change the outcome basis?

**Inputs (characterize before deciding):** using existing captured ticks plus a targeted quote-density characterization, measure the fraction of eligible strikes reaching ≥80% FRESH coverage over the outcome horizon, broken down by moneyness, DTE, and time-of-day. The v1 evidence (2 of 4 strikes near-unquotable) is the seed, not the conclusion — it is too small to decide alone.

**Decision branches:**
- **A1 — viable with liquidity-aware selection:** enough eligible strikes clear coverage that a liquidity-first selector can reliably pick labelable contracts → keep the executable-bid basis, proceed to B/C.
- **A2 — not viable:** even the best-quoted eligible strikes don't clear coverage reliably → v2 must adopt a **different, pre-registered outcome basis** (e.g., underlying-mapped P&L via greeks), explicitly accepting the loss of "pure realized option bid" purity. Specify that basis and its bias controls before B/C.

**Do not proceed to Stage B until A returns A1 or a fully-specified A2 basis.**

## Stage B — Direction definition (rarity)

Only after Stage A.

**First-class risk — widen-vs-edge tradeoff.** Loosening `momentum_slowing` / `causal_confirmation` raises the fire rate but almost certainly **lowers the average edge per signal** — weaker reversals get admitted. It is plausible the strict gate was strict *because the clean reversals are where any edge lives*. So v2 could reach 200 episodes and find nothing, from dilution rather than a wrong concept.

**Design objective (stated precisely):** produce *enough* distinct, mechanically-causal reversal moments to be studyable prospectively **while preserving as much per-signal edge as possible** — not "maximize fires." Prefer the *smallest* relaxation that reaches the fire-rate target.

- Candidate relaxations of the two binding groups are enumerated and pre-registered (the capped variant set).
- No relaxation may make the setup non-causal (no look-ahead; completed-bar only).

## Stage C — Contract-selection / labelability

- Liquidity-first selection, with **delta as a constrained target** rather than the primary optimizer.
- **Selection-bias caveat (new):** always preferring the most-quoted strike systematically shifts *which* contracts (and thus which exposures/populations) get measured — e.g., toward round-number or ATM strikes. This bias must be recorded and, where possible, controlled, so v2 isn't quietly measuring a different thing than v1.
- If even liquidity-first keeps landing on near-unquotable strikes, that routes back to Stage A2 (change the outcome basis).

## Review gate (now with numbers)

Happens after the v2 design is concrete on paper, **before any code or data-collection change.**

**Quantitative pass targets (proposals — operator to confirm before freeze):**
- **Fire rate:** v2 must plausibly produce **≥ ~2 distinct, non-overlapping reversal moments per session** (roughly 3× the v1 rate) so that ≥200 non-overlapping episodes are reachable in a ~4–6 month prospective window (~60–125 sessions). State the assumed rate and derive the timeline explicitly.
- **Labelability:** **≥ ~80% of selected contracts** must clear the 80% FRESH coverage bar (vs the ~50% coin flip observed). If Stage A chose A2, restate the coverage/labelability target for that basis.
- **Discipline preserved:** design shows no threshold gaming, no cohort reuse, capped/pre-registered variants.

**Gate inputs:** frozen v2 cohort spec; explicit direction rules; explicit contract-selection policy; explicit coverage/outcome-basis target; explicit failure modes; explicit no-lock conditions; the pre-registered variant set and count.

**Pass condition:** the design is worth implementing only if it plausibly clears **both** the fire-rate and labelability targets **without** redefining staleness away or reusing the current cohort's basis.

## No-lock conditions (carried into the eventual cohort)

- < 200 non-overlapping labeled episodes.
- Coverage/labelability below the confirmed target.
- Any post-hoc parameter change after the prospective lock (→ new cohort).
- Primary metric not cleared against the session-block matched null + walk-forward.

## Current decision state

Stage A is now complete and lands on **A2**: the current single-option executable-bid outcome basis is not reliably viable as specified. The next design move is therefore to pre-register an alternative outcome basis before any further direction/selection implementation. Everything downstream remains gated on that decision.
