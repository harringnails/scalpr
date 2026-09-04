# Multi-Instrument Signal Study v0

This is an isolated paper/shadow study for SPY, QQQ, IWM, and DIA. It has no trade, admission, order, server, collector, A2-store, or Guard integration.

## Automated Session

The existing session LaunchAgent starts at 03:55 ET (02:55 on the operator's Central-time Mac). Before RTH, only the batched Alpaca SIP capture runs so D1/H1a can observe a premarket reclaim. At RTH open the unchanged SPY context/pin capture starts, alongside the four-symbol FlashAlpha five-minute capture. After close, the unchanged SPY A/B evaluators run first; the multi-instrument evaluator and report run afterward.

The market stream writes `multi_instrument_market_v0.jsonl`; structures write `multi_instrument_flashalpha_v0.jsonl`; evaluations write `multi_instrument_signal_v0.jsonl`; and the summary writes `multi_instrument_signal_report_v0.json`. All are gitignored and isolated.

## Rate Gate

`python multi_instrument_signal_v0.py rate-check` must return `fits: true`. Growth's observed limit is 2,500 requests/day. The new RTH FlashAlpha stream uses 948 calls; the conservative total including the unchanged SPY poll and option-chain entry is 1,186, leaving 1,314 calls headroom. A 429 is logged and stops the new FlashAlpha loop.

## Point-in-Time Discipline

Every FlashAlpha endpoint retains provider/request/response timestamps and raw response provenance. Its provider timestamp is aligned backward-only to the same instrument's clean Alpaca SIP quote within five seconds. Missing, crossed, stale, or unaligned data remains unavailable for that instrument only. Outcomes use the same strict anchor plus 5/15/30/60-minute midpoint rule; missing stays missing.

The report remains `UNDERPOWERED` until each frozen arm/cohort reaches N=150. `REPLICATED` is secondary only. Nothing in this module is a trade signal.
