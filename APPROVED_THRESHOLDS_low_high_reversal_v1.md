# Approved Thresholds — `low_reversal_v1` (CALL) & `high_reversal_v1` (PUT)

**Operator confirmed: 2026-08-06.** Applies to **both mirrored cohorts**. This is **threshold approval only** — it is NOT a cohort lock and authorizes no restart. `operator_confirmed` in the cohort JSON stays gated on the separate pre-open lock.

## Approved values

| Field | Approved | Note |
|---|---:|---|
| `execution.max_spread_pct` | **6.0** | tightened from 8.0 for cleaner execution |
| `execution.max_ask_price` | **5.00** | raised from 2.50 so the cohort genuinely spans 0–2 DTE (captured data: $2.50 excluded 60% of the 2 DTE sample; all 2 DTE < $5.00) |
| `outcome.option_risk_fraction` | **0.20** | stop = ask×0.80; with 1.5R, target = ask×1.30 |

## Retained (already agreed — unchanged from draft)

`min_contract_volume` 200 · `min_open_interest` 500 (established-strike bias explicitly accepted) · `bid_poll_interval_seconds` 5.0 · `minimum_coverage_fraction` 0.80 · delta band 0.40–0.50 (target 0.45) · DTE 0–2 · `reward_risk` 1.5 · direction rules unchanged (1.5× 5-min ATR extension, 0.15% level proximity, Wilder RSI14 @ 35, 3-bar higher-low/reclaim confirmation).

`target_session` — **remains null until the pre-open lock.**

## Frozen cost model (per `TRANSACTION_COST_MODEL_v1.md`)

- Entry at executable **ask**, exit measured on executable **bid** (spread already captured — do not re-add).
- Regulatory allowance **$0.12 round-trip / contract** (conservative vs ~$0.104 itemized).
- Slippage **1 tick/side primary**; report **0 / 1 / 2-tick sensitivity**; edge must survive 2 ticks.
- Return convention **`net_return_fraction`** (decimal, matches engine); `exit_effective = max(0, exit_bid − slippage)`.
- Version/hash stamps on every outcome record.

## What this unblocks (in order, per the Rev 3 activation order)

1. Codex implements + tests the **real cost model** in `entry_bid_capture_v1.py` (replace the hardcoded `PLACEHOLDER`/`None`; compute `net_return_fraction` at 0/1/2 ticks with the floor).
2. `NoTradeTrackingPlan` + shared rejection/trade episode admission → typed gate results → **pre-lock evidence-manifest capture** → separate hypothetical store.
3. Tests + implementation hashes stamped.
4. **Operator separately approves the pre-open cohort lock** (sets `target_session`, `operator_confirmed: true`, writes the fail-closed registry entry) — before an open, account flat.

## Boundaries

- No cohort lock, no restart, no live enablement from this approval.
- Any later change to these values → a **new cohort id** (never relock existing content).
- Stamp these into both cohort JSONs' `execution`/`outcome` blocks and clear the corresponding `unconfirmed_fields` entries; leave `operator_confirmed: false` until the pre-open lock.
