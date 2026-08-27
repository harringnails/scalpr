# A2 Dense Source Operator Runbook

**Scope:** operator-only authenticated A2 re-label and capture-gap characterization.
Research-only; no server restart, collector interaction, cohort action, Guard access,
or order path.

## Preconditions

- Run from `/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7`.
- Use the operative `.venv`.
- Load the existing Keychain-backed Alpaca paper credentials in the same Terminal.
- Historical stock quote/trade entitlement must include SIP.

## Uniform dense-source re-label

```bash
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
source ./load_keychain_env.sh
.venv/bin/python a2_dense_source.py relabel
```

This reads all admitted episodes, excludes quarantined rows, deduplicates immutable
episode keys, excludes cross-side timestamp collisions, and applies the unchanged
anchor plus 5/15/30/60-minute basis uniformly. Provider failures remain
`A2-UNAVAILABLE`; no value is imputed.

## Capture-gap bias characterization

```bash
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
source ./load_keychain_env.sh
.venv/bin/python a2_dense_source.py bias-check --recent-sessions 10
```

The output is characterization only: `GAPS_RANDOM`,
`GAPS_CONDITION_CORRELATED`, or `UNAVAILABLE`. It has no cohort or edge effect.

## Verify the re-label

```bash
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
.venv/bin/python a2_dense_source.py verify
```

`status` must be `PASS`. The verifier requires:

- every current-run label has `endpoint_source = alpaca_historical_stock_quote_v1`;
- the frozen 5-second maximum and all four fixed horizons remain present;
- dense availability exceeds the legacy tick-log cross-check;
- every `A2-UNAVAILABLE` to `A2-AVAILABLE` flip has an anchor and four endpoint
  timestamps at or before their boundaries, each with age from 0 through 5 seconds.

Outputs:

- `v2_data/a2_measurement/a2_labels_v2.jsonl`
- `v2_data/a2_measurement/a2_summary_v2.json`
- `v2_data/a2_measurement/a2_dense_source_comparison_v0.json`
- `v2_data/a2_measurement/a2_capture_gap_bias_v0.json`
