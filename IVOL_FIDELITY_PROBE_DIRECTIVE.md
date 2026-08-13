# IVOL Fidelity Probe — Run Directive (for Codex)

**Status: research annex, read-only. No broker/order/Guard authority anywhere in this workflow.** Implements §3 of `IVOLATILITY_RESEARCH_ANNEX_PROPOSAL.md`. Paper/shadow discipline unchanged. Nothing here clears a cohort lock or produces an edge claim.

**What this is:** a two-file harness that compares IVolatility's historical intraday minute quotes against a reference feed, to decide whether IVOL is faithful enough to build a pre-screen on. It answers the two open ceilings (quote provenance + 1-minute resolution) empirically.

**Files delivered:**
- `ivol_fidelity_probe.py` — env-agnostic comparison engine (pure stdlib). Computes the three read-offs and emits a stamped JSON report. Verified running.
- `ivol_pull_minute_chain.py` — runs **in the IVOL VS Code env**; pulls one contract's intraday minute chain to the handoff CSV.
- `ivol_fidelity_probe_config.example.json` — config template.

---

## Environment split (important)

The two data sources live in different places, so the workflow is: **pull IVOL data in the IVOL env → export CSV → run the comparison where the reference lives (Scalpr host).** The comparison engine is pure-stdlib and imports nothing Scalpr-specific, so it runs in either environment.

- **IVOL VS Code env:** run `ivol_pull_minute_chain.py` (needs `IVOLATILITY_API_KEY`). Produces `timestamp, option_symbol, bid, ask, bid_size, ask_size`.
- **Scalpr host:** run `ivol_fidelity_probe.py` with the IVOL CSV + the reference. **Scalpr's Alpaca/UW keys never enter the IVOL env**, and IVOL's key never leaves it.

---

## Two-stage run (the timing boundary is hard)

### Stage P — Proxy (do now, PRELIMINARY ONLY)
- `stage: "P"`, `reference_kind: "alpaca_csv"`.
- Reference = **Alpaca historical option quotes** for the exact same OSI contract and minutes, exported to CSV (`timestamp, bid, ask[, bid_size, ask_size]`, UTC). Use Alpaca's historical **option quotes** endpoint (OPRA), aggregated to last-quote-per-minute.
- **A Stage-P pass CANNOT clear the gate.** The report will always show `gate_cleared: false` at Stage P by construction. Its only power is to *fail early*: `early_warning: true` (exit code 1) means IVOL and our proxy already disagree, which is worth knowing before Monday.
- Label/file all Stage-P outputs `PRELIMINARY_PROXY_REFERENCE` (the report does this automatically).

### Stage D — Definitive (just after Monday, GATE-CLEARING)
- `stage: "D"`, `reference_kind: "scalpr_capture_jsonl"`, `reference_path: entry_intelligence_bid_ticks_v1.jsonl`.
- Reference = Monday's (or any later clean session's) fresh capture; the loader keeps only `status == "FRESH"` rows for the contract.
- **Only Stage D can set `gate_cleared: true`.** It requires `level_fidelity.pass` AND `coverage_quality.pass`.

Downstream annex work (pre-screen, vol-regime lab, cost check) begins **only** after a Stage-D `gate_cleared: true`.

---

## The three read-offs + pass rules (proposed thresholds — sanity-check the numbers)

1. **Level fidelity** (`level_fidelity`): median absolute IVOL−reference difference on shared, mutually-usable minutes. Pass = median abs diff ≤ `max_median_abs_diff_ticks` (default 1 tick) for **both** bid and ask, **no systematic bias** (|median signed diff| ≤ `max_median_signed_bias_ticks`, default 0.5 tick), and ≥ `min_compared_minutes` (default 20). *A one-tick-average agreement with no directional lean kills the provenance worry empirically.*
2. **Coverage/quality** (`coverage_quality`): fraction of IVOL minutes with a usable two-sided quote (`bid>0 and ask>0 and ask>=bid`). Pass = ≥ `min_usable_minute_fraction` (default 0.90). Zero-bid / one-sided / crossed minutes are counted, never treated as fills (§K).
3. **Resolution sensitivity** (`resolution_sensitivity`): for each sample reversal instance, first-hit (stop-vs-target, **stop checked first** — matches `entry_bid_capture_v1.evaluate_outcome`) is labeled at MINUTE_1 and at MINUTE_5-derived bars; agreement is reported. Informational: any disagreement quantifies how much the 1-minute limit matters for this setup. (Pull the MINUTE_1 chain; MINUTE_5 is derived by last-in-5-min-bucket, so one pull covers both.)

---

## Gotchas to pin (Codex review, annex §8)

- **Timezone frame is explicit.** Every source declares its timestamp tz; the engine converts to UTC before flooring to a minute. **Confirm the tz of IVOL's intraday `timestamp`** before trusting Stage P — run `ivol_pull_minute_chain.py --emit-tz-note` and set `ivol_timestamp_tz` accordingly (`America/New_York` vs `UTC`). A wrong tz will manufacture a fake divergence.
- **`eod_patch_1545` / EOD endpoints:** do **not** source 0–2 DTE quotes from any EOD path — use the intraday minute endpoint only. (The pull script uses `/equities/intraday/single-equity-option-rawiv`.)
- **Split / corporate-action:** SPY 0–2 DTE over a single session shouldn't hit one, but confirm neither series applies an adjustment that shifts the strike/underlying alignment.
- **`intraday_mode`:** not used by this probe (the probe doesn't run the backtest engine), but keep it explicit when the pre-screen is later built.
- **Cost math:** the probe compares raw quotes only; it does **not** apply the cost model. The $0.12 + tick-slippage overlay is a later pre-screen concern, not a fidelity concern.

---

## Acceptance for this directive
- `ivol_pull_minute_chain.py` returns a non-empty minute CSV for a chosen SPY 0–2 DTE contract, tz confirmed.
- `ivol_fidelity_probe.py` runs on (IVOL CSV, Alpaca proxy CSV) and emits a `PRELIMINARY_PROXY_REFERENCE` report; `gate_cleared` is `false` by construction; `early_warning` correctly reflects divergence.
- No broker/order/Guard import in either script; IVOL key confined to the IVOL env; Alpaca/UW keys confined to the Scalpr host.
- Open questions for you: confirm IVOL intraday `timestamp` tz; confirm the Alpaca historical-option-quote request shape you use for the proxy export; and whether the leg-cost math has anything to pin before the *pre-screen* (not this probe).
