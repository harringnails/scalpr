# Outcome-Basis Reconciliation Note

**Status:** clarification / pre-registration alignment. **No new relaxation.** Paper/shadow. Reaffirms the frozen A2 decision and separates two outcome bases that have drifted apart in the live dry-run.

## The problem this fixes

The Aug-14 dry-run reported NO-GO because the one admitted episode was `NO_POINT_IN_TIME_CONTRACT` → 0 eligible contracts, 0 bid ticks, 0 outcomes. Read literally, that looks like "no progress." It isn't. The failure is on a basis the research already retired.

## Two distinct outcome bases — do not conflate

**1. Single-option executable-bid basis — RETIRED.** `STAGE_A_OUTCOME_BASIS_VIABILITY.md` verdict is **A2: not viable** (quote density is structurally bimodal; the selected strike is often near-unquotable). The contract-data-v2 dry-run's trackability metrics — `NO_POINT_IN_TIME_CONTRACT`, eligible-contract selection, executable-bid ticks, completed option outcomes — are **all** this basis. Its GO/NO-GO is a statement about option-bid trackability, and it may be **structurally un-lockable**. That is acceptable and expected, not a regression.

**2. Underlying-forward-return basis — the MVP edge basis.** `A2_OUTCOME_BASIS_SPEC.md` (frozen) labels `signed_return_60m` off point-in-time SPY mids via `a2_measurement`. It needs **no option contract**, so `NO_POINT_IN_TIME_CONTRACT` does **not** block it.

## The rule (clarification, not relaxation)

An admitted reversal episode is eligible for the **A2 edge cohort** if it is:

- admitted by the frozen v1.2 detector, **not quarantined**, **not** part of a cross-side collision, and
- covered by SPY quotes across its 60-minute horizon (anchor + endpoints present).

**Option trackability is irrelevant to this eligibility.** If SPY coverage is missing → the episode is **A2-UNAVAILABLE** (missing stays missing; §K), not admitted to the inferential set. No double counting: each episode counts once, under the existing non-overlap / episode-key discipline.

## The MVP progress metric (state it plainly)

Progress toward the edge verdict = **count of clean, A2-labelable episodes accruing toward ≥200** — *not* option-bid trackability. On Aug-14 that count moved **0 → 1** (pending confirmation that SPY quotes cover the 11:55 ET episode's 60-minute horizon). The contract-data-v2 NO-GO is a separate scoreboard.

## Audit wording rule

In daily audits, label the retired option-bid scoreboard as **informational / expected-empty** whenever the admitted episode is `NO_POINT_IN_TIME_CONTRACT`. Do not present that scoreboard as the study go/no-go. The study gate is the A2 accrual and post-warmup freshness path only.

## What does NOT change

- The A2 edge test still requires: frozen detector, non-overlap dedup, session-block matched null (`p=(k+1)/(n+1)`), 4-fold chronological walk-forward, ≥200 episodes, and the frozen verdict rule. This note changes **none** of that inferential bar.
- Cohorts remain **unlocked**. No cohort lock is implied or authorized here.
- The option-bid / contract-data-v2 study may continue as a **separate, more-ambitious track** answering its own question (can option-bid tracking ever clear coverage). It does **not** gate the MVP edge test, and its NO-GO must not be reported as if it were the edge verdict.

## Action for Codex

1. Route every admitted, non-quarantined, non-collision episode through `a2_measurement` (underlying-return) **regardless** of option trackability; record `signed_return_60m` or `A2-UNAVAILABLE`.
2. Add a distinct **`clean_a2_labelable_episode_count`** as the accrual metric toward the ≥200 gate.
3. In the daily audit, report the two scoreboards **separately and labeled** — option-bid trackability (contract-data-v2) and A2 accrual (MVP edge) — so they are never conflated again.
4. Confirm whether the Aug-14 admitted episode is A2-labelable (SPY horizon coverage) and report it as clean-A2 episode #1 or `A2-UNAVAILABLE` with the reason.
