# FlashAlpha Shadow Ingestion v0

Status: READ-ONLY, advisory, observational, non-qualifying.

## Isolation Contract

`flashalpha_shadow_v0.py` only reads FlashAlpha HTTP endpoints and appends to
`flashalpha_shadow_v0.jsonl`. It has no imports from the collector, server,
Guard, gate, admission, A2, dense, prospective, broker, or order paths. Every
record carries `advisory_only: true`, `observational_only: true`,
`is_qualifying: false`, `admission_authority: false`, and
`execution_authority: false`.

The JSONL file is isolated runtime evidence and is covered by the repository's
`*.jsonl` ignore rule plus an exact ignore entry. Records are append-only,
point-in-time stamped, and carry a canonical SHA-256 hash. Provider errors, tier
restrictions, malformed JSON, network failures, and rate limits are recorded
with `returned_values: null`.

## Confirmed Endpoint Semantics

Base URL: `https://lab.flashalpha.com/v1`.

| Name | Path | Documented access |
| --- | --- | --- |
| `gex` | `/exposure/gex/{symbol}` | Free equities; Basic+ ETFs/indexes |
| `levels` | `/exposure/levels/{symbol}` | Free equities; Basic+ ETFs/indexes |
| `maxpain` | `/maxpain/{symbol}` | Basic+ |
| `zero_dte` | `/exposure/zero-dte/{symbol}` | Growth+ |
| `flow_pin_risk` | `/flow/pin-risk/{symbol}` | Growth+ live flow pin score |

The canonical 0DTE endpoint is `exposure/zero-dte`; its response contains a
`pin_risk` object. `flow/pin-risk` is a distinct flow-derived endpoint. They are
logged as separate observations and never substituted for one another.

## Rate Discipline

The default endpoint is `levels` and the default call budget is one. There are
no automatic HTTP retries. A `429` observation is appended and the run stops
immediately. To request more calls, the operator must explicitly raise
`--budget` and list the endpoints.

The Free plan permits five requests per day. The provider resets that allowance
at 00:00 UTC. SPY, SPX, and QQQ analytics require paid ETF/index entitlement;
restricted responses remain explicit observations.

## Operator Command

Run one real, read-only SPY levels request using the Keychain entry. This spends
at most one API call and writes only the ignored shadow ledger:

```zsh
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-flashalpha-shadow-operative"
"/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/.venv/bin/python" \
  flashalpha_shadow_v0.py \
  --symbols SPY \
  --endpoints levels \
  --budget 1 \
  --tier FREE \
  --output flashalpha_shadow_v0.jsonl
```

To pull the full requested set after confirming a sufficient tier and quota,
explicitly select `gex,levels,maxpain,zero_dte` and set `--budget 4`. Add
`flow_pin_risk` only when the separate flow-derived observation is wanted.
