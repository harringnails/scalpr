# Threshold Walkthrough — `low_reversal_v1` (operator approval aid)

Advisory only. This walks each `unconfirmed_fields` entry with a recommendation, the reasoning, and the trade-off, so you can approve as-is or adjust before the lock. Nothing here changes the JSON or locks anything. Assume SPY ≈ $740 (from recent journal strikes) for the sizing math.

**Two fields carry a real design choice — read those first: `max_ask_price` (§5) and `option_risk_fraction` (§6).**

---

### 1. `target_session` — *proposed: null*
**Recommend: leave null now; set at lock time to the next full RTH session after (a) operator confirmation and (b) verified dry-run bid coverage.** It cannot be a past or partial session, and dry-run data never carries in. This is the one field that *should* stay null until you actually lock. No change needed now.

### 2. `execution.max_spread_pct` — *proposed: 8.0*
**Recommend: tighten to 6.0 (acceptable to keep 8.0).** For a ~0.45-delta SPY 0–2 DTE contract, real spreads run ~1–3% in liquid conditions; 8% only shows up in fast markets or thin 0DTE strikes. The spread is *already* fully penalized in the outcome (you enter at ask, exit at bid), so 8% isn't "unsafe" — it just admits marginal, noisier-execution candidates. Tightening to ~6% keeps the cohort to genuinely tradeable contracts and reduces spread noise dominating the bid-path. Trade-off: fewer candidates, slightly slower to reach the 200 gate.

### 3. `execution.min_contract_volume` — *proposed: 200*
**Recommend: keep 200 (fine as-is).** SPY ATM 0–2 DTE routinely trades thousands of contracts, so 200 is a low but harmless floor that only screens out genuinely dead strikes. Could raise to 500 for a bit more liquidity assurance; not necessary. Trade-off: negligible.

### 4. `execution.min_open_interest` — *proposed: 500*
**Recommend: keep 500, but accept the established-strike bias explicitly.** *Correction (my earlier note was factually wrong):* open interest does **not** build intraday. OCC sets OI after end-of-day clearing; what rises during the session is **volume**, not OI. So an OI ≥ 500 floor doesn't "delay" newly listed strikes — it can **exclude them for the entire session** until their OI is established next day. That's a real selection effect: the cohort is biased toward established strikes. It's an acceptable bias (established strikes are the liquid ones), but it should be stated in the JSON, not left implicit. Trade-off: newly listed strikes — including some fresh 0DTE lines — are simply out for the day.

### 5. `execution.max_ask_price` — *proposed: 2.50* — **DESIGN CHOICE**
**Recommend: raise to $5.00 if the cohort genuinely means 0–2 DTE.** Two corrections to how I framed this before:

*Outlay ≠ planned risk (Correction 2).* `max_ask_price` caps **premium outlay** (the cash to buy the contract), not planned risk. At $2.50 the outlay is $250 and the planned risk — outlay × `option_risk_fraction` (0.20) — is ~$50. At $5.00 the outlay is $500 and the planned risk ~$100. Keep those two concepts separate: raising the ask cap raises position *size*, and the 20% stop scales the *risk* off it. If your risk-per-trade target is what matters, it's the product, not the ask cap alone.

*The cap silently filters DTE — and the captured data confirms it.* In the latest IVolatility snapshot, among 1–2 DTE contracts passing delta 0.40–0.50 / vol 200 / OI 500: all three 1 DTE contracts were ≤ $2.50, but only **2 of 5** 2 DTE contracts were ≤ $2.50 — so $2.50 excluded **60% of the 2 DTE sample**. All five 2 DTE contracts were **below $5.00**. So a $5.00 cap keeps 0–2 DTE representative; $2.50 skews the cohort to 0–1 DTE. (Caveat: that's a tiny EOD IVol sample — treat the distribution as provisional until we have prospective intraday OPRA observations.) Choose deliberately: **$5.00** for a true 0–2 DTE cohort, or keep **$2.50** and label the cohort 0–1 DTE by construction.

### 6. `outcome.option_risk_fraction` — *proposed: 0.20* — **DESIGN CHOICE (most consequential)**
**Recommend: keep 0.20, with eyes open — this single number shapes the whole outcome distribution.** It sets the native option stop at `ask × 0.80` (a 20% option drawdown) and, with 1.5R, the target at `ask × 1.30` (+30%). Trade-offs: a **tighter** value (0.15) exits faster and turns more episodes into stop-firsts — safer per trade but more premature exits on normal 0DTE noise; a **looser** value (0.25–0.30) gives the thesis more room but risks larger give-back. 20% is a reasonable middle for an option scalp and pairs cleanly with 1.5R. The thing to know: because this defines both stop and (via R) target, it's the parameter most likely to be second-guessed after seeing results — which is exactly why it must be frozen *before* the session, not tuned to the data.

### 7. `outcome.bid_poll_interval_seconds` — *proposed: 5.0*
**Recommend: keep 5.0 (acceptable to go 2–3).** With the 15s max-fresh-gap and 80% coverage rules, 5s sampling balances precision against load. The only cost of 5s is that a very brief wick that touches target/stop *between* polls can be missed — but the same-timestamp collision rule already resolves `STOP_FIRST` (conservative), so the miss risk skews against overstating wins. Fine as-is; drop to 2–3s only if you want finer touch detection. Trade-off: more data/requests at finer intervals.

### 8. `outcome.minimum_coverage_fraction` — *proposed: 0.80*
**Recommend: keep 0.80 (fine as-is).** Requiring 80% of the outcome window covered by fresh bids before a completed no-hit label is a sensible balance — high enough that a labeled outcome is trustworthy, not so high that normal quote gaps make most episodes UNLABELABLE. Interacts with the 15s gap rule. Trade-off: raising to 0.90 yields cleaner but fewer labels; lowering to 0.70 the reverse.

---

## Converged provisional choices (yours + mine agree)

| Field | Provisional value | Note |
|---|---|---|
| `max_spread_pct` | **6.0** | tightened from 8.0 for execution cleanliness |
| `min_contract_volume` | **200** | fine as-is |
| `min_open_interest` | **500** | **explicitly accept the established-strike bias** (§4) |
| `max_ask_price` | **5.00** | for a genuine 0–2 DTE cohort (§5); data shows $2.50 excluded 60% of 2 DTE |
| `option_risk_fraction` | **0.20** | drives the outcome distribution; confirm deliberately |
| `bid_poll_interval_seconds` | **5.0** | fine as-is |
| `minimum_coverage_fraction` | **0.80** | fine as-is |
| `target_session` | **null** | stays null until the actual lock |

## Still NOT ready to freeze — blockers before lock

Even with thresholds converged, the cohort can't lock until:
1. The **cost model is implemented in the engine** (`entry_bid_capture_v1.py` still hardcodes `PLACEHOLDER`/`None`; net returns + the 0/1/2-tick sensitivity must be computed) — see `TRANSACTION_COST_MODEL_v1.md`.
2. The **return-unit convention** (decimal fraction) is frozen and named in code + JSON.
3. Implementation hashes stamped, offline tests green (incl. the cost-model calc), `operator_confirmed: true`, target session set, and the fail-closed lock-registry entry written pre-open.

If these provisional values are your final calls, say so and I'll assemble the clean "approved thresholds + cost model" package for Codex to implement and stamp — I won't lock anything; that stays your pre-open step.
