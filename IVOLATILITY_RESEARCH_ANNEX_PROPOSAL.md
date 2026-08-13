# IVolatility Research Annex — Proposal (for Codex feedback)

**Status: PROPOSAL / DRAFT — not a directive, not approved. Retrospective research only. Paper/shadow discipline unchanged.** This document is circulated to gather Codex's thoughts and additions before anything is built. Nothing here locks a cohort, feeds a frozen record, or produces an edge claim. Please challenge, correct, and add.

**One-line intent:** use IVolatility's historical intraday options data + backtest engine as a **retrospective pre-screen and volatility-regime lab** that generates *pre-registered candidate hypotheses* for the existing prospective pipeline — never as a substitute for prospective validation, and never as a path for a non-registered signal to reach a decision.

---

## 0. Why this, why now

Prospective validation is slow by design: a frozen cohort needs ≥200 non-overlapping executable-bid episodes, which is many sessions. Before spending months of prospective collection on the `low_reversal_v1` / `high_reversal_v1` hypothesis (or any future conditioned variant), a clean historical backtest can cheaply tell us whether the setup even has a pulse. The asymmetry is the whole value:

- A backtest that shows **nothing** across years of history is a strong reason **not** to burn prospective collection time.
- A backtest that shows **something** is **not proof** (in-sample, retrospective) — but it upgrades the hypothesis from "untested guess" to "worth the prospective 200-episode gate."

This is exactly the §J posture: retrospective work produces candidates, prospective work produces edge claims. The annex must not blur that line.

---

## 1. What IVolatility actually provides

Two independent investigations converged on the picture below. Most of it is established from IVOL's API/prompt **documentation** plus the engine source those digs read; the items that remain **inferences** (notably quote provenance, §1c) are flagged as such. Codex's code-level confirmation of what is and isn't verified is recorded in §8.

### 1a. Data — genuine intraday two-sided quotes
- Endpoints: `/equities/intraday/single-equity-option-rawiv` (single contract) and `/equities/intraday/equity-options-rawiv` (full chain, returns a download URL).
- Granularity: `MINUTE_1` (~410 bars/day), `MINUTE_5` (~82), `MINUTE_15`, `MINUTE_30`, `HOUR`, `CUSTOM` (a fixed minute-of-hour).
- Per-bar fields include **separate** `optionBidPrice` / `optionAskPrice` **and** `optionBidSize` / `optionAskSize`, plus per-bar IV/greeks, `timestamp`, and OSI `optionSymbol`. **Not** mid-only; **not** EOD-only.
- 0-DTE rows exist in the historical database (`dteFrom=0` supported).

### 1b. Engine — general framework, conservative fills
- `run_backtest(strategy_function, config)` simply calls the user's `strategy_function`. The day loop, entry conditions, and exit conditions are all user-authored. IV signals (Z-score, IV Rank/Percentile, term structure), price/technical helpers (RSI, ATR, Bollinger, MACD, realized-vol), and earnings filters are **optional helpers, not a forced framework**. A mechanical reversal rule drops straight into the entry condition.
- **Fill geometry is hardcoded to cross the spread**: long fills at **ASK**, short fills at **BID** (`calculate_entry_cost:6375`, `calculate_close_cost:6278`); native `use_bid_ask: True`. There is **no `fill_mode`/mid option** and no way to invert it. This is *precisely* our ask-entry / fresh-bid-exit geometry, and it means results cannot be flattered by mid-fills.
- Stop-loss is **optional** (`intraday_mode` defaults to `disabled`; SL is one of several opt-in exit types). The 9:31/16:19 earnings-SL timing is a config recipe, not a global rule.
- **No commission model**; slippage is represented only by spread-crossing plus a 1.5× capital-at-risk buffer that affects **sizing, not P&L**.

### 1c. The two honest ceilings (both investigations independently found these)
1. **1-minute resolution, no intrabar sequencing.** Bars carry bid/ask at bar granularity, not tick-by-tick. You cannot see which of stop/target was touched *first within a minute*, and you can miss an intrabar touch-and-revert entirely. This directly affects first-hit win/loss labeling.
2. **Quote provenance is undocumented.** The intraday feed is the "rawiv" family. IVOL distinguishes NBBO-labeled endpoints from rawiv endpoints, and applies "NBBO"/"executable" language only to its **real-time** layer, not the historical one. So historical intraday bid/ask is a stored quote whose fidelity to what we'd actually have transacted at is **not guaranteed by the docs**.
   - *Interpretation note — explicitly tentative:* "rawiv" most plausibly means **raw implied volatility** (IVOL's un-smoothed, closer-to-market product, versus their interpolated IV surface), which *would* argue against heavy modeling of the bid/ask. **This is an inference from naming, not a confirmed library fact.** Code review (§8) confirms the stored `bid`/`ask` are preserved as recorded fields (not synthetic fills), but does **not** confirm they are exchange/NBBO-executable quotes. Provenance therefore stays **unverified pending the §3 probe** — neither red flag nor green light.

### 1d. Data-hygiene facts to respect
- Intraday minute bars are **US-only**; non-US underlyings fall back to EOD.
- The EOD snapshot is a 16:19 after-hours print (market-makers widen the ask; underlying is after-hours). The engine explicitly patches it to the 16:00 / 15:45 regular-hours value. **Do not build 0–2 DTE geometry off the EOD endpoints** — use the intraday minute endpoint.
- 0-DTE / illiquid strikes can show zero-bid, one-sided, or stale quotes. These must be filtered and *counted*, never silently filled (see §4, §K).

---

## 2. Scoped use (what the annex is FOR)

Three uses, in priority order, all retrospective and all walled off from frozen records:

1. **Base-hypothesis pre-screen.** Characterize whether the frozen mechanical reversal setup shows any historical signal on US SPY 0–2 DTE, using our exact ask-entry / fresh-bid-exit geometry, before committing prospective collection.
2. **Volatility-regime lab.** Use IV Z-Score / IV Percentile (IVOL's native strength) to characterize whether reversal outcomes differ across vol regimes — producing a **pre-registered** conditioned candidate (`reversal × vol-regime`) for a future frozen cohort per §J.
3. **Cost-model reality check.** Compare real historical SPY 0–2 DTE spreads against our frozen cost model ($0.12 round-trip regulatory + 0/1/2-tick slippage on top of spread-crossing).

Explicitly **not** for: producing edge claims, tuning parameters to pick a "best" configuration, or letting any IVOL-derived signal reach a Scalpr decision axis or gate.

---

## 3. THE GATE — quote-fidelity + resolution probe (must pass before any modeling)

Both remaining ceilings (§1c) are resolved by **one empirical test**. Build nothing else until this passes.

**Probe design:**
- Pull IVOL `MINUTE_1` `bid`/`ask`/sizes for **one specific SPY 0–2 DTE contract** (exact OSI symbol), across a defined window (e.g., one RTH session, or the ~60-minute outcome windows around a few real reversal instances).
- Compare **quote-by-quote, minute-by-minute** against the **feed we would actually transact on** — our own Alpaca/OPRA capture for the same contract and minutes.

**Three read-offs, with pass/fail thresholds (proposed — Codex to sanity-check the numbers):**

1. **Level fidelity.** Median absolute difference between IVOL and our reference bid (and ask) ≤ ~1 tick ($0.01), with no systematic directional bias (IVOL not persistently tighter/wider). *Pass* → the provenance worry is empirically dead regardless of what "rawiv" means. *Material divergence* → the backtest is a rough screen only, and we label it as such.
2. **Coverage/quality.** Fraction of minutes where IVOL's quote is zero-bid, one-sided, or stale. Report it; define the filter (`bid>0 and ask>0`, freshness) that the backtest will apply. This must never be papered over as a fill.
3. **Resolution sensitivity.** Re-label first-hit (stop vs target) at `MINUTE_1` vs `MINUTE_5` on the sample reversal instances; measure how much the win/loss labels move. Small → 1-minute resolution is adequate for this setup. Large → the setup genuinely needs sub-minute fills and IVOL can't be the final word.

**Reference-data dependency (sequencing):** the cleanest reference is our own fresh executable-bid capture, which realistically means the *definitive* comparison happens **just downstream of the Monday dry run** once trackable bids exist. A sooner proxy is pulling Alpaca's own historical option quotes for the same contract/minutes, but the truest comparison is against the feed we'll actually trade on. **To be explicit: the Monday dry run does not itself validate IVOL fidelity** — it only produces a higher-quality reference to compare IVOL against. Fidelity is established only by the comparison in this probe, never by Monday's capture alone.

**Timing boundary — explicit two-stage run (unambiguous).** The probe runs twice, and the two runs do **not** carry equal weight:

- **Stage P — Proxy check (pre-Monday, PRELIMINARY ONLY).** Reference = Alpaca **historical** option quotes for the same contract/minutes (a weaker stand-in for our live feed). Purpose: an early smoke-test of level agreement and coverage while we wait for a real capture. **A passing proxy does NOT clear the §3 gate.** A *failing* proxy is a useful early warning that something is off. Every Stage-P output is labeled `PRELIMINARY_PROXY_REFERENCE` and is never cited as fidelity validation, never used to justify building the pre-screen, and never counted toward the gate.
- **Stage D — Definitive check (post-Monday, GATE-CLEARING).** Reference = Monday's (or any later clean session's) fresh executable-bid capture (`entry_intelligence_bid_ticks_v1.jsonl`, `status == "FRESH"` rows). This is the **only** run that can clear the §3 gate and permit the §4 pre-screen.

**The gate is cleared by Stage D only.** Stage P can *fail* the plan early (worth knowing now) but can never *pass* it. No pre-screen, vol-regime lab, or cost-check work begins until Stage D returns known fidelity.

---

## 4. Research-annex discipline (maps to §I / §J / §K of the flow amendments)

Non-negotiable if the annex is used:

- **§I (firewall):** everything IVOL is a non-qualifying research input. No IVOL-derived value or signal reaches `direction`, `quality`, `executability`, any act/no-act tier, or any gate. It produces candidate hypotheses only.
- **§J (sample budget + multiple comparisons):** a backtest is hypothesis-generation, not edge. Any promising result becomes a **pre-registered** condition that still faces the prospective ≥200-episode-per-family gate on live data. Pre-register the hypothesis + parameters **before** running; use hold-out years / walk-forward; **the `optimization_results/` grid is used only for sensitivity plots, never to select-and-claim** a best parameter set. Record the number of conditions examined.
- **§K (data-state honesty):** zero-bid / one-sided / stale quotes stay distinct and are filtered + counted, never silently filled (mirrors the snapshot-`volume:0` lesson). "Bar has no usable two-sided quote" is a recorded state, not a phantom fill.
- **Cost-model reconciliation:** IVOL's spread-crossing (buy ask / sell bid) **is the executed fill** — the spread cost is already captured there. Our **$0.12 round-trip regulatory fee + 0/1/2-tick slippage** is a **Scalpr-side comparison overlay applied to net P&L only** — not a second fill, not a change to IVOL's prices — so the spread is **not** double-counted. IVOL's 1.5× buffer is sizing only and is ignored for per-trade net. Both frameworks agree on the dominant cost (crossing the spread).
- **Independence is a feature:** IVOL is a *different* vendor than our live Alpaca/OPRA feed, so a later live confirmation is genuine independent corroboration — but results are never byte-comparable and are never merged into Scalpr's records.
- **Isolation/security:** the annex lives in the IVOL VS Code environment, fully separate from the Scalpr server. Scalpr's Alpaca/UW keys never touch that environment. No annex code imports or touches broker/order/Guard paths.

---

## 5. Reproducibility

Version + hash every backtest config (hypothesis id, parameters, date range, resolution, filters, cost overlay) the way Scalpr hashes everything, so any result is reproducible and tied to a frozen spec. A backtest result with no frozen config hash is not citable.

---

## 6. Questions for Codex (please weigh in)

1. **Fidelity probe design:** is the quote-by-quote comparison sound, and are the proposed thresholds (≤1 tick median abs diff, no directional bias) the right bar? Is there a better reference source than our own Alpaca/OPRA capture — and can we get a usable reference *before* Monday without weakening the test?
2. **"rawiv" provenance:** can you confirm from the library/data (not just the prompt docs) whether the historical intraday bid/ask is a recorded quote snapshot vs anything derived? Is my "raw IV = closer to market" reading correct?
3. **First-hit resolution:** beyond treating bar resolution as a sensitivity axis, is there a better/conservative first-hit labeling rule for 1-minute data (e.g., default-to-stop on an ambiguous same-bar double-touch, matching our live stop-before-target tiebreak)?
4. **Engine gotchas that could contaminate a 0–2 DTE intraday study.** *(Codex-answered, see §8):* pin **timezone handling** (repo mixes UTC/ET — the probe must state its comparison frame), **`eod_patch_1545`** (real contamination risk if EOD endpoints touch 0–2 DTE logic), **split / corporate-action adjustment** (pin whether historical fields are adjusted and whether underlying alignment is affected), and **`intraday_mode`** (a config choice that distorts results if left implicit). **DuckDB caching** is *not* elevated — no confirmed stale-read bug in the inspected files; revisit only if one surfaces. Still open for Codex: anything in the leg-cost math worth pinning.
5. **Cost-model reconciliation:** is layering our $0.12 + tick slippage on top of IVOL spread-crossing the correct way to make results comparable, or is there double-counting to avoid?
6. **Discipline:** anything in §4 that is too loose (a leak risk) or too strict (blocking legitimate research)?

---

## 7. Proposed first action (pending feedback + approval)

**Do not build the reversal backtest yet.** The immediate work is the §3 quote-fidelity + resolution probe, run in two stages per the timing boundary above:

1. **Now (Stage P, proxy):** build the probe harness and run it against Alpaca historical option quotes for a preliminary, clearly-labeled `PRELIMINARY_PROXY_REFERENCE` read. This can only *fail* the plan early; it cannot clear the gate.
2. **Just after Monday (Stage D, definitive):** re-run the same harness against Monday's fresh executable-bid capture. **Only this run clears the §3 gate.**

Everything downstream (pre-screen, vol-regime lab, cost check) is gated on **Stage D** returning known fidelity. If Stage D shows material divergence, the annex is demoted to a rough directional screen and labeled accordingly; if it passes, we proceed to a single pre-registered pre-screen under §4.

---

## 8. Codex review — incorporated (this pass)

Codex reviewed the draft; outcomes recorded here.

**Code-level confirmation (answers Q2).** The local library confirms IVOL is treated as a **read-only** data source. `ivolatility_adapter.py` uses `/equities/intraday/single-equity-option-rawiv` and `/equities/eod/stock-opts-by-param`; the validation script also pulls `/equities/dl/options-rawiv` and `/equities/rt/options-rawiv`. `options_intelligence.py` normalizes IVOL rows into a deterministic snapshot, preserving `bid`/`ask`/IV/greeks as **stored fields, not synthetic fills**. What the code **cannot** confirm: that the historical intraday `rawiv` bid/ask is a quote in the exchange/NBBO sense, or that it would have been **executable** at the time. **Net: read-only path and stored two-sided fields are confirmed; executable quote provenance is not — it stays unverified pending the §3 probe.**

**Wording tightened per review.** Removed the overstated "verified by two independent investigations" (§1); kept the "rawiv = closer to market" reading explicitly as an *inference, not a library fact* (§1c); clarified that the Monday dry run supplies a better *reference*, not fidelity *validation* (§3); clarified the cost overlay is a Scalpr-side comparison layer, not a second fill (§4).

**Engine gotchas narrowed (answers Q4).** Confirmed set to pin: timezone frame, `eod_patch_1545`, split/corporate-action adjustment, `intraday_mode`. DuckDB caching not elevated absent an observed stale-read bug.

**Unchanged.** §3 remains the gate; the annex stays a proposal; provenance stays unverified pending the probe.
