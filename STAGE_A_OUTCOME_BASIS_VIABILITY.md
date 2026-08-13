# Stage A — Outcome-Basis Viability Analysis (the gating step for Reversal v2)

**Status:** analysis spec. Characterization only — no new cohort, no lock, no development. Paper/shadow.
**Gates:** all of Reversal v2 (per `REVERSAL_V2_DESIGN_BRIEF.md`). Nothing downstream is designed until this returns A1 or a fully-specified A2.

## The one question this answers

Can single-option executable-bid tracking clear the 80% FRESH coverage bar reliably enough to be a valid outcome basis on SPY 0–2 DTE strikes?

- **A1 — viable:** enough eligible strikes clear coverage that a liquidity-first selector can reliably pick labelable contracts → keep the executable-bid basis; proceed to direction + selection design.
- **A2 — not viable:** even the best-quoted eligible strikes don't clear coverage → v2 needs a different, pre-registered outcome basis (e.g., underlying-mapped P&L via greeks). Specify that before any further v2 work.

This is a **binary gate**. The whole point is to avoid building direction/selection code on an outcome basis that can't work.

## What "coverage" means (fixed, from the current study)

- Outcome horizon: **60 minutes** from entry.
- Poll cadence: **5 s** → 720 intervals/horizon.
- A quote is FRESH when age ≤ 5 s, two-sided, not crossed (apply the shipped clock-skew tolerance).
- **Coverage** = fraction of the horizon's 5-s intervals with a FRESH quote.
- A contract **clears** when coverage ≥ **80%** (the current `minimum_coverage_fraction`). Do not change this threshold — Stage A measures against it, it does not redefine it.

## Measurement design

For a representative grid of SPY 0–2 DTE contracts, over the outcome horizon, compute per-contract coverage and tabulate the fraction that **clear**, bucketed by the variables that plausibly drive quote density.

- **Strike grid:** span moneyness from roughly |delta| 0.30 → 0.55 (ATM ± several strikes), both rights, so we see whether the target-delta band (~0.45) is quoted as densely as pure ATM.
- **Sessions:** ~15–20 recent RTH sessions (enough to average over calm/active days).
- **Entry times:** sample multiple horizon start points per session — e.g. open (09:30–10:00), midday (12:00–13:00), power hour (15:00–15:30) — because quote density likely varies by time-of-day.
- **DTE:** separate 0 / 1 / 2 DTE (0-DTE liquidity behaves differently near expiry).

**Primary data source:** Alpaca historical **option quotes** (OPRA, `data.alpaca.markets`) — the *same feed the study uses*, so it's representative. Pull quotes for each grid contract over each sampled horizon, reconstruct 5-s-interval freshness, compute coverage. (Authenticated pull → runs from the operator's Terminal, where the Keychain creds load; not from a credential-less agent shell.)

*Alternative (slower):* extend the live collector to sample the strike grid prospectively over coming sessions. Historical pull is faster for a one-time characterization; use the live sampler only if historical option-quote depth is insufficient.

## Read-offs

Produce a coverage-clearance table:

| bucket | contracts sampled | % clearing 80% coverage | median coverage |
|---|---|---|---|
| by moneyness (ATM, ~0.45Δ, wings) | | | |
| by DTE (0/1/2) | | | |
| by time-of-day (open/mid/power-hour) | | | |
| **liquidity-first (best eligible strike per moment)** | | | |

The **liquidity-first row is the decisive one**: if the selector were allowed to pick the best-quoted eligible strike at each moment, what fraction of horizons clear coverage?

## Pass / fail (proposals — operator confirms before acting)

- **A1 if** the liquidity-first selection clears coverage in **≥ ~80%** of sampled horizons (vs the ~50% coin flip seen in the v1 seed). Then: single-option tracking is viable; delta becomes a *constrained* target within a liquidity-first policy.
- **A2 if** even liquidity-first clears **< ~80%** (or clears only in a narrow bucket, e.g. ATM-at-midday). Then: single-option tracking is not reliably viable; v2 must adopt a different outcome basis, specified and bias-controlled before further work.

## Bias to record (not to hide)

Liquidity-first selection systematically favors the most-quoted strikes (often round numbers / pure ATM), which shifts *which exposure* is measured versus the v1 delta-targeted population. Record the selected-strike distribution so v2 isn't quietly studying a different thing than v1.

## Preliminary seed (from existing captures — do NOT treat as the answer)

The four contracts we already have freshness data on are strikingly bimodal: two ~80% FRESH, two ~3% (after the clock fix). That is 4 contracts, far too small to decide — it only motivates the full grid above. It hints the answer is **not** a clean A1, which is exactly why Stage A must be run properly before any v2 build.

## Discipline

- Characterization only. No cohort, no lock, no threshold change, no v2 code.
- Uses existing/historical data; no live-trading, no Guard/order path.
- Result is a gate verdict (A1 / A2), not an edge claim.

## Gate Verdict

**A2 — not viable as currently specified.**

The broader freshness characterization closes the gate on the current single-option executable-bid outcome basis:

- The clock-skew artifact is fixed, so the remaining staleness is not a collector bug.
- Coverage is still structurally bimodal across strikes.
- The selected strike can be good or nearly unquotable by chance, which makes labelability unstable even when the setup is valid.

That means the current outcome basis cannot be assumed reliable enough to support the next-stage reversal study without a different, pre-registered outcome-basis design.

## Immediate Implication

Stage B and C remain gated. The only honest next design move is to specify and pre-register an A2 alternative outcome basis, then re-run the review gate on fresh data.
