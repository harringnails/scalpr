# Quant Core Databento shadow feed for Scalpr

This is a separate, default-off observer beside the legacy service. It does not import
`scalp_server.py`, Guard, broker, order, liquidation, pause/resume, or dashboard code. It
does not replace Alpaca. Every output states `execution_authority=false` and
`order_routing_permitted=false`.

The observer consumes the same immutable Quant Core OPRA snapshot as SPY Hedge, but requests
the independently scoped `scalpr` consumer view. It retains only Scalpr's automated SPY 0–2
DTE envelope and records every contract's provider timestamp, receipt timestamp, quote age,
resolution, bid/ask, spread gate, and OI provenance in an append-only hash-chained JSONL file.

## Historical packet check

From `Scalpr7/`, use the Quant Core environment and enable this one process explicitly:

```bash
SCALPR_QUANT_CORE_SHADOW_ENABLED=1 \
PYTHONPATH="/Users/natalieharrington/Documents/ChatGPT/Quant Core v1" \
"/Users/natalieharrington/Documents/ChatGPT/Quant Core v1/.venv/bin/python" \
  quant_core_shadow_feed.py historical \
  --packet-dir "/Users/natalieharrington/Documents/ChatGPT/Quant Core v1/data/spy_hedge/2026-08-14"
```

The default evidence file is `quant_core_opra_shadow_v1.jsonl`, which is ignored by Git under
the repository-wide `*.jsonl` rule. Replaying the same snapshot is idempotent.

Historical `cbbo-1m` observations can be `RESEARCH_READY`, but they can never pass the live
executability gate because that gate requires tick resolution, quote age at most five
seconds, two-sided non-crossed quotes, and spread at most 6%.

## Live observer

After `DATABENTO_API_KEY` is injected securely into the process environment, run during OPRA
hours:

```bash
SCALPR_QUANT_CORE_SHADOW_ENABLED=1 \
PYTHONPATH="/Users/natalieharrington/Documents/ChatGPT/Quant Core v1" \
"/Users/natalieharrington/Documents/ChatGPT/Quant Core v1/.venv/bin/python" \
  quant_core_shadow_feed.py live --seconds 30
```

Do not place the key in this command, source, JSONL, screenshots, or chat. The sidecar is not
started by `restart.sh`; the running legacy paper service remains unchanged. Promotion beyond
observer status requires a separate reconciliation cohort against Scalpr's current Alpaca
OPRA observations and a reviewed activation decision.

## Reconcile against existing Alpaca OPRA evidence

When the Databento observer and `entry_intelligence_bid_ticks_v1.jsonl` cover the same
contracts and timestamps, compute descriptive level differences without declaring a pass or
promoting either source:

```bash
SCALPR_QUANT_CORE_SHADOW_ENABLED=1 \
"/Users/natalieharrington/Documents/ChatGPT/Quant Core v1/.venv/bin/python" \
  quant_core_shadow_feed.py reconcile \
  --alpaca-log entry_intelligence_bid_ticks_v1.jsonl \
  --tolerance-seconds 5
```

The reconciliation artifact is append-only and hash-chained. Its
`promotion_eligible` field is always `false`; thresholds and a qualifying cohort must be
frozen and reviewed separately before any source promotion.
