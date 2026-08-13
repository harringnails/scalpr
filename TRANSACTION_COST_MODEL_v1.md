# Transaction-Cost Model v1 — SPY 0–2 DTE options (Alpaca), for Entry Intelligence outcomes

Purpose: replace the `PLACEHOLDER_EXPLORATORY_ONLY` cost model so the cohort's primary metric — *net* executable-bid return vs abstaining — can be computed. Deliberately **slightly conservative** (never understate costs, or we'd overstate edge). Advisory; **not ready to freeze** — see the open implementation items at the end. Rev 2 incorporates Codex's review corrections.

## What the outcome path already captures — do NOT double-count

The label enters at the **executable ask** and measures exits on the **executable bid**, so the **full bid–ask spread is already paid** in every outcome. The cost model adds only **regulatory fees** and **slippage beyond the quoted touch**. Adding the spread again would double-charge it.

## Return-unit convention (frozen, named) — Correction 4

The engine (`entry_bid_capture_v1.py`) reports **decimal fractions** (e.g. `0.1847`), not percentages. To match it, the canonical unit is:

- **`net_return_fraction`** — decimal (0.1847 = +18.47%). Canonical, stored.
- **`net_return_pct`** — `net_return_fraction × 100`. **Display only**, never stored as the metric.

One convention, named, matching the code. The threshold/cost docs previously mixed `18.47` and `0.1847`; the decimal fraction wins.

## Component 1 — Commission: **$0.00** (verified)

Alpaca charges **no commission** on options for ordinary self-directed Trading API accounts (partner/Elite tiers can differ). Only pass-through regulatory fees apply.

## Component 2 — Regulatory / clearing fees (per contract), from the Alpaca fee schedule

| Fee | When | Rate | Round-trip, 1 contract |
|---|---|---|---|
| FINRA TAF | sell only | $0.00329 / contract | $0.00329 |
| ORF (options exchanges) | buy + sell | $0.02295 / side | $0.04590 |
| OCC clearing | buy + sell | $0.025 / side | $0.05000 |
| CAT (FINRA-CAT) | buy + sell | $0.00 currently | $0.00000 |
| SEC Section 31 | sell only | per-$1M of proceeds (rate periodically adjusted) | ~$0.005 on a $250 sale |
| **Actual total** | | | **≈ $0.104 round trip** |

**Frozen value: `regulatory_fees_round_trip_per_contract_usd = 0.12`** — a conservative fixed constant slightly above the ≈$0.104 actual, so the model never understates. True up ORF/OCC/Section-31 from a real Alpaca fee statement before any "best" claim.

## Component 3 — Slippage beyond the touch (an assumption, not proven conservative) — Correction 3

The label assumes fills *at* the touch (buy at ask, sell at bid). Real marketable orders don't always get it. **SPY options trade in $0.01 penny increments regardless of price**, so one tick = $0.01/share = $1.00/contract — and this holds even if `max_ask_price` rises above $3.

Calling one tick "conservative" is an assumption, not a proof. So:

- **Primary model:** `slippage_ticks_per_side = 1`.
- **Preregister a sensitivity sweep:** compute the metric at **0, 1, and 2 ticks/side**.
- **Robustness rule:** the edge must **survive 2 ticks/side** before it's considered robust. If it only exists at 0 ticks, it isn't real.

## Component 4 — Negative-proceeds floor — Correction 5

An option bid can't go below zero, and slippage must not push proceeds negative:

```
exit_effective = max(0.0, exit_bid − slippage_per_side)
```

## Net-return formula (decimal fraction, per contract in $/share)

```
entry_effective = entry_ask + slippage_per_side
exit_effective  = max(0.0, exit_bid − slippage_per_side)          # Correction 5
fees_per_share  = regulatory_fees_round_trip_per_contract / 100    # 0.12 / 100 = 0.0012
net_return_fraction = ( (exit_effective − entry_effective) − fees_per_share ) / entry_effective
```
Abstain baseline = explicit **0.0** (no costs when flat). Primary metric = `mean(net_return_fraction of taken CALLs) − 0`.

## Worked example + sensitivity (SPY ≈ $740, 0DTE call, entry ask 1.50, exit bid 1.80)

| slippage/side | entry_eff | exit_eff | net_return_fraction |
|---|---|---|---|
| 0 tick | 1.50 | 1.80 | **0.1992** |
| 1 tick (primary) | 1.51 | 1.79 | **0.1846** |
| 2 tick | 1.52 | 1.78 | **0.1703** |

Gross was +0.20; costs shave it to ~0.199 / 0.185 / 0.170. Here the edge survives 2 ticks — that's the bar. On a losing move the same haircut makes losses slightly worse; both directions are penalized.

## Frozen parameters to stamp into the cohort JSON

```json
"cost_model": {
  "cost_model_version": "entry-intel-cost-model-v1",
  "return_convention": "decimal_fraction",
  "commission_per_contract_usd": 0.00,
  "regulatory_fees_round_trip_per_contract_usd": 0.12,
  "regulatory_itemization_usd": {"taf_sell": 0.00329, "orf_per_side": 0.02295,
    "occ_per_side": 0.025, "cat": 0.0, "sec_section31_sell_est": 0.005,
    "actual_round_trip_est": 0.104},
  "slippage_ticks_per_side_primary": 1,
  "slippage_sensitivity_ticks_per_side": [0, 1, 2],
  "robustness_rule": "edge must survive 2 ticks/side",
  "tick_size_usd_per_share": 0.01,
  "tick_note": "SPY options penny increments regardless of price",
  "negative_proceeds_floor": "exit_effective = max(0, exit_bid - slippage_per_side)",
  "spread_handling": "already_captured_by_ask_entry_bid_exit_do_not_readd",
  "abstain_baseline_net_return": 0.0,
  "engine_implementation_required": true
}
```

## NOT operational yet — required before freeze (Correction 6)

**Stamping the JSON is necessary but not sufficient.** `entry_bid_capture_v1.py` (lines ~196–197) hardcodes `cost_model_status: "PLACEHOLDER"` and `net_return_after_realistic_costs: None`, and emits only `gross_executable_return`. Before this cost model is real, the engine must:

1. Read the frozen `cost_model` params.
2. Compute `net_return_fraction` from the gross path with the `max(0, …)` floor, at **each** of 0/1/2 ticks/side.
3. Replace the hardcoded `PLACEHOLDER`/`None` with the computed net and a `cost_model_version` stamp.
4. Emit the sensitivity trio (0/1/2 ticks) so the robustness rule can be checked.

That's a Codex engine change, plus offline tests, before the cohort can produce net outcomes.

## Honest caveats

- This is a *model*, not measured fills. The slippage assumption is the biggest lever; keeping it conservative and sensitivity-tested protects against overstating edge.
- Fees are pennies; spread (already in the path) and slippage dominate. Widening `max_spread_pct` raises realized cost through the bid-path — another reason to keep that gate tight.
- Freeze with the rest of the cohort; changing any cost parameter after results forces a new cohort id.

## Sources

- [Alpaca — options commission ($0, self-directed Trading API)](https://alpaca.markets/support/what-are-the-commission-fees-per-option-contract)
- [Alpaca — brokerage fee schedule (TAF/ORF/OCC/CAT rates)](https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf)
- [Alpaca — options pricing increments (SPY penny increments)](https://alpaca.markets/support/options-pricing-increments-and-options-order-handling)
- [Alpaca Docs — Regulatory Fees](https://docs.alpaca.markets/us/docs/regulatory-fees)
- [FINRA — Trading Activity Fee (TAF)](https://www.finra.org/rules-guidance/guidance/faqs/trading-activity-fee)
