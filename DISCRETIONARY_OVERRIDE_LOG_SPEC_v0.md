# Discretionary Override / Outcome Log — Pre-Registered Spec v0

**Status:** pre-registered design spec. Characterization/measurement only. Paper/shadow.
**Mode:** paper / shadow only. Advisory. **No gate, no sizing, no order authority.**
**Scope:** a **separate evidence pool** that records each discretionary entry decision and its outcome, so the claim *"my read beats the precheck panel"* becomes a testable hypothesis instead of an anecdote.

## Why this exists

The precheck is, by its own definition, a descriptive non-predictive evidence panel that never blocks a trade. An operator override that worked once is `n=1` — it cannot tell skill from luck. This pool turns overrides into measured evidence: does overriding the panel actually add signed edge, on the same A2 outcome basis used everywhere else?

## What this is NOT

- **Not** a gate, filter, or sizing input. It only records.
- **Not** part of any frozen cohort. It is its own pool (per `EVIDENCE_POOLS_DISCIPLINE.md`) — never pooled with the automated reversal episodes or the regime measurement.
- **Not** a fast verdict. Discretionary volume is low; this pool will sit `UNDERPOWERED` for a long time, and that is honest, not a flaw.
- **Not** a change to the precheck, the detector, the regime layer, or admission.

## The population (pre-register to kill survivorship bias)

Log **every discretionary entry decision for which a precheck reading existed** — not just the overrides that worked. That means all four cells:

| | Took the trade | Skipped the trade |
|---|---|---|
| **Followed precheck** | logged | logged |
| **Overrode precheck** | logged | logged |

If only "overrides that felt right" are logged, the pool is worthless — it just re-remembers winners. The inclusion rule is: *a precheck reading was displayed and the operator made a decision.* No post-hoc selection of which decisions to record.

## Record schema (one row per decision, append-only)

- `decision_id`, `decided_at` (point-in-time), `symbol`, `trade_type`, `implied_direction` (bullish/bearish → sign +1/−1)
- **Precheck block:** `precheck_version`, headline `decision` (YES/NO/NO READ), `agreement_pct`, tactical `coverage_pct`, per-horizon breakdown. If unavailable → `precheck_status: MISSING` (never imputed).
- **Operator action (categorical, required):** `FOLLOWED_TOOK` / `FOLLOWED_SKIPPED` / `OVERRODE_TOOK` / `OVERRODE_SKIPPED` / `NO_READ_TOOK` / `NO_READ_SKIPPED`.
- `operator_rationale` (optional free text — e.g., "regime trending down, momentum slowing"). Never required for the test; for review only.
- **Regime block:** `regime_tag` as-of `decided_at` from `regime_layer_v0.classify_regime` (`UNKNOWN` stays `UNKNOWN`).
- **Outcome block (filled in later, automatically):** A2 `signed_return_60m` (primary) + `5/15/30m` auxiliary via `a2_measurement`, anchored at `decided_at`; `outcome_status` AVAILABLE/UNAVAILABLE; **missing stays missing**.
- `pool: DISCRETIONARY_OVERRIDE_LOG`, `advisory_only: true`, `execution_authority: false`, `part_of_frozen_cohort: false`.

## Capture discipline (the anti-bias rules — non-negotiable)

1. **Log at decision time**, before the outcome is known. The row is created when the decision is made.
2. **The outcome is filled in later by the labeler**, not by the operator. The operator never edits a row after seeing how it turned out.
3. **Skipped trades still get a `decided_at` and a hypothetical anchor** so their A2 outcome can be labeled — otherwise "the ones I correctly skipped" are invisible and the comparison is biased toward taken trades.
4. Records are append-only; corrections are new rows referencing the original, never in-place edits.

## Pre-registered analysis (freeze before accumulating)

Primary hypothesis: **does overriding the precheck add signed edge?**

- **Primary statistic:** mean `signed_return_60m` of the `OVERRODE_TOOK` rows.
- **Null:** session-block matched (`entry-episode-session-block-sign-null-v1` pattern), `p = (k+1)/(n+1)`.
- **Baseline for contrast:** compute the same statistic for `FOLLOWED_TOOK` rows, so the panel's own skill is measured on the same basis. The honest outcomes include "neither has edge."
- **Walk-forward:** temporal split with sign-consistency on held-out folds (fold count scaled to N; at very low N, report "suggestive, not confirmed" rather than inventing folds).
- **Pre-registered N floors (operator confirms before freeze):** `< 50` overrides → `UNDERPOWERED` (report count only, no verdict); `≥ 50` → preliminary read; `≥ 100` with matched-null `p ≤ 0.05` and held-out sign-consistency → a real verdict.
- **Verdict:** `OVERRIDE_EDGE` / `NO_OVERRIDE_EDGE` / `UNDERPOWERED`. No "my read beats the panel" claim until it clears — same standard as every other signal.

Secondary (only if the pool grows): condition the override result by `regime_tag` — but that multiplies comparisons (§J) and needs far more rows, so it is not the primary question.

## Data-state honesty (§K)

- `precheck_status: MISSING`, `regime_tag: UNKNOWN`, and `outcome_status: UNAVAILABLE` are distinct and never collapsed into a generic pass or a zero.
- `UNKNOWN`/`MISSING` rows are excluded from the inferential statistic but their rate is always reported.

## Non-negotiables

- Advisory only; no gate, no sizing, no order/Guard authority.
- Separate pool; never merged with reversal episodes or regime measurement.
- Log at decision time; outcome auto-filled; no post-outcome edits.
- Missing stays missing; verdict floors and null pre-registered, not softened.
- Paper/shadow only.

## Reuse / implementation notes

- Precheck output: `precheck.py`. Regime tag: `regime_layer_v0.classify_regime`. Outcome labels: `a2_measurement` (same signed A2 basis). New append-only pool file (gitignored `*.jsonl`).
- No broker/order/Guard imports. Unit tests: population completeness (all four cells loggable), log-at-decision then label-later flow, missing precheck → MISSING (not imputed), skipped-trade outcome labeling, `UNDERPOWERED` below the N floor.
