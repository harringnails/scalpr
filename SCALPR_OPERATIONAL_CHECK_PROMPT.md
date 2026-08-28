# Scalpr — Operational Readiness Check (prompt for Codex / any agent)

Paste everything below into Codex.

---

You are verifying that my local Scalpr trading server is fully operational.
Do not modify code. Do not place, close, or modify any trade. Report findings only.

## Hard constraints — read first

1. **All commands must run in my own macOS Terminal session.** `127.0.0.1:8420`
   and the Keychain credentials are reachable *only* from my Mac login session.
   If you run curl from a sandbox/agent environment it will report every endpoint
   as unreachable even when the server is perfectly healthy. If you cannot run in
   my Terminal, give me the commands to paste and interpret what I paste back.
2. **Ground truth is `ps` + `lsof` + a Terminal curl.** If any status summary
   disagrees with those three, the summary is wrong.
3. **Never send Ctrl-C to the window running the server.** It kills the platform.
   Use a separate Terminal window for all checks.
4. **Never kill the server without first proving the account is flat** via
   `/api/account-flat-proof`. Killing it while it holds a position orphans that
   position with no Guard supervision. `restart.sh` performs this check and
   refuses unsafe restarts — prefer it over a raw `kill`.
5. **Never weaken, bypass, or edit the flat-account startup guard.** If startup is
   blocked by `RuntimeError: Startup blocked: Alpaca reports N open position(s)`,
   the correct fix is for me to close the position in the Alpaca paper dashboard.
6. `/api/status` does not exist in this build. `/api/precheck` requires a
   `?symbol=` query parameter.

## Step 1 — Is it running?

```
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
ps aux | grep scalp_server.py | grep -v grep
lsof -n -P -iTCP:8420 -sTCP:LISTEN
tail -20 scalpr_server_manual.log
date
```

Expect a Python PID bound to `127.0.0.1:8420 (LISTEN)`.

## Step 2 — Core trading path

```
curl -s http://127.0.0.1:8420/api/state | python3 -m json.tool
curl -s http://127.0.0.1:8420/api/account-flat-proof | python3 -m json.tool
curl -s 'http://127.0.0.1:8420/api/precheck?symbol=SPY' | python3 -m json.tool
curl -s http://127.0.0.1:8420/api/storage/health | python3 -m json.tool
```

Green means:

- `state`: `mode: paper`, `feed: "SIP"`, `feed_error: null`,
  `guard_loop_healthy: true`, `unprotected_symbols: []`
- `account-flat-proof`: `source: alpaca_trading_api_direct_uncached`,
  `account_status: ACTIVE`, `flat: true`, `positions_count: 0`,
  `open_orders_count: 0`
- `storage/health`: `status: OK`, `disk_free_percent` comfortably above 15

A `feed` of `IEX` means the SIP entitlement probe failed and it silently fell
back — flag this, do not treat it as green.

## Step 3 — Study / capture layer

```
curl -s http://127.0.0.1:8420/api/entry-intelligence/collector | python3 -m json.tool
curl -s http://127.0.0.1:8420/api/v2/options-intelligence/status | python3 -m json.tool
curl -s http://127.0.0.1:8420/api/v2/institutional-flow/status | python3 -m json.tool
curl -s http://127.0.0.1:8420/api/v2/cloudsql/status | python3 -m json.tool
```

Green means:

- Entry Intelligence: `enabled: true`, `state: ACTIVE_RTH_CAPTURE` during RTH
  (`DISABLED_DEFAULT_OFF` means the flag was never set)
- IVolatility: `enabled: true`, `capture_running: true`,
  `reason: scheduled_eod_capture`
- Unusual Whales: `status: AVAILABLE`, `ingestion_running: true`,
  `last_ingestion` timestamped today, `last_error: null`
- Cloud SQL: `worker_running: true`, and `mirror.updated_at` dated **today**
  (a stale date means the worker is up but not actually mirroring)

Safety fields should read `execution_authority: false`, `guard_access: false`,
`paper_shadow_only: true` on every study endpoint. Flag it loudly if not.

## Step 3b — Separate the two scoreboards

The daily audit must report these as distinct scoreboards and must not conflate them:

- `contract-data v2` / executable-bid trackability: retired option-bid basis, informational only. A `NO_POINT_IN_TIME_CONTRACT` episode, zero contracts, zero bid ticks, or zero outcomes is expected-empty and does **not** gate the MVP.
- `A2 accrual` / post-warmup freshness: the actual study gate. Report clean A2-labelable episode count and post-warmup direction-axis freshness separately.

When checking direction freshness, verify whether `MISSING` rows are warmup-only or genuinely post-warmup by timestamp. If the `MISSING` rows fall inside the warmup window, say so explicitly and do not treat that as a freshness regression.

## Step 4 — Accumulation checks (later in the session)

```
curl -s http://127.0.0.1:8420/api/entry-intelligence/collector | python3 -m json.tool | grep -E 'bid_records|decision_packets|updated_at'
curl -s http://127.0.0.1:8420/api/v2/cloudsql/status | python3 -m json.tool | grep updated_at
```

`bid_records` should rise above 0 as qualifying entries appear. Still 0 near the
close, with `decision_packets` climbing, is worth investigating.

## If the capture layer is OFF

Every subsystem defaults to OFF unless its exact env var is `1`. `restart.sh`
sets them:

| Subsystem | Flag |
|---|---|
| Entry Intelligence bid collector | `ENTRY_INTEL_BID_CAPTURE_ENABLED=1` |
| Unusual Whales ingestion | `SCALPR_UW_INGESTION_ENABLED=1` |
| IVolatility EOD capture | `SCALPR_IVOL_CAPTURE_ENABLED=1` |
| Cloud SQL mirror worker | `SCALPR_CLOUDSQL_MIRROR_ENABLED=1` |

`restart.sh` also exports `WAVE_RIDING_ENABLED=1` and
`INCUBATION_SHADOW_ENABLED=1` (lines 54 and 59). On reversal dry-run days those
must be OFF — comment those two lines out or unset the vars.

Cloud SQL additionally requires `.venv/bin/python` and
`v2_data/cloudsql_profile.json` to exist, or it logs DISABLED regardless of flag.

## Output format

Give me a short table: subsystem, status (GREEN / DEGRADED / OFF), and the one
field that proves it. Then list only the items that are not green, with the
specific fix for each. Do not restart anything without telling me first.
