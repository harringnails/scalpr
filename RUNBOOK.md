# Scalpr Local Runbook

## Safety status

Legacy is a local paper service. Do not start it with `--live`. Do not copy or inspect `scalp.keys` during development.

## Verify the existing service

1. Open `http://localhost:8420`.
2. Confirm the header says PAPER.
3. Confirm Guarded Positions and Holdings before changing or restarting anything.
4. Treat pause, resume, sell, liquidate, and engage actions as broker-affecting operations even in paper mode.

## Start legacy manually

From `Scalpr7/`, run `bash restart.sh` while the market is closed and the PAPER
account is flat. The operator-run script launches the server as
`caffeinate -is .venv/bin/python scalp_server.py --sip`, preventing idle and
system sleep only while that server process remains alive. It is not an
auto-restart daemon; after any process exit, the normal off-session flat-account
restart gate still applies. The script enables both shadow observers and can
encounter Alpaca 429 limits. Review any broker error immediately.

## Unusual Whales Workup credential

Run `python3 install_uw_token.py` in a regular macOS Terminal. The installer asks
only for the raw Public API token, validates it directly with Unusual Whales, and
stores it in macOS Keychain only after a successful response. Never paste the
token into chat, source files, command arguments, or `scalp.keys`.

## IVolatility credential

Run `python3 install_ivolatility_key.py` in a regular macOS Terminal. The
installer accepts the raw API key through a hidden prompt, validates read-only
SPY option-chain access, and stores the key under `scalpr.ivolatility.api` in
macOS Keychain. Scalpr loads it as `IVOLATILITY_API_KEY`. Never paste it into
chat, source files, command arguments, `scalp.keys`, logs, fixtures, or reports.
Key availability does not start capture; options intelligence remains explicitly
operator-disabled until its entitlement matrix passes.

## Stop/restart cautions

- Guard state is not persistent. Restarting while a position is open can leave the broker position unguarded.
- Never restart solely to apply documentation or test changes.
- `restart.sh` and `restart_cron.sh` now refuse to stop the service unless the
  local API successfully verifies that Alpaca holdings are empty. They use a
  graceful stop and refuse to fall back to `kill -9`.

## Credential incident

The local key file was exposed in a shared archive. Rotation is pending an Alpaca MFA support ticket. Until rotation:

- Keep the file local and ignored.
- Do not display, copy, archive, upload, or commit it.
- Do not create new credential files.
- Record rotation completion in `DECISIONS.md` without recording key material.

## Raw evidence

Raw CSV/JSONL/state directories are the source for historical metrics. Keep them read-only during migration. Derived datasets must include source paths, hashes, schema version, and generation time.

## Guard execution-data contract

- Option and stock Guards advance from a positive executable bid only. Midpoint, ask, and last trade are never Guard prices.
- `NO_BID` or `QUOTE_MISSING` means **unprotected**: price, profit, peak, and breach counters do not change until a bid returns.
- Re-engaging a paused Guard fails closed until a current executable bid exists.
- Every new trade requires a per-submission acknowledgement that the Guard may
  automatically sell 100% of the position. The acknowledgement resets after a
  successful entry.
- Re-engaging a paused Guard requires typing the exact contract symbol. The
  backend rejects bare resume requests even if an old dashboard tab sends one.
- The active card labels the controls `Pause automatic sells` and `Enable
  automatic sells`; enabling is recorded as `dashboard_resume_confirmed` with
  a request ID. A later `guard_ratchet` sell is a separate audited event.
- Every 15 seconds, a successful broker position read retires Guards whose positions are absent. A failed broker read never retires a Guard.
- The Guard is created immediately after a confirmed entry fill. Entry signals
  and shadow-study setup happen afterward in the research worker.
- Wave, incubation, and climb-add work never runs in the Guard polling thread.
- The dashboard Guard Safety banner is red whenever the execution loop is stale,
  a Guard is paused, or an executable bid is unavailable.
- Repeated Engage requests for an already guarded contract do not add quantity.
  Use the explicit Add contracts control; it is paper-only and requires a
  protected Guard plus broker quantity reconciliation.
- Execution logs identify the initiator and request ID for automatic ratchet,
  manual sell, manual liquidation, initial buy, and explicit add actions.

## Scope and storage

### Optional Cloud SQL V2 evidence store

Cloud SQL is opt-in and never implicitly replaces local evidence files or the
SQLite mirror. The configured target is `scalpr-dev:us-central1:scalpr-dev`,
database `scalpr`, over the public Cloud SQL Connector path with IAM database
authentication disabled. Google Application Default Credentials authorize the
connector; a normal PostgreSQL username/password authorizes the DB session.
The connector delegates certificate verification to macOS Keychain through
Python Truststore so locally trusted issuers are honored. A verified macOS PEM
or Certifi bundle remains the portable fallback; TLS verification is never
disabled.

1. Install the Google Cloud CLI from Google's official checksum-verified macOS
   package, then run `gcloud auth application-default login`.
2. Ensure that Google identity has the Cloud SQL Client role in project
   `scalpr-dev`.
3. Run `.venv/bin/python setup_cloudsql.py` here. Enter the PostgreSQL username
   and password at the hidden prompt. The password is validated before it is
   saved to macOS Keychain; only a non-secret profile is written under ignored
   `v2_data/`.
4. Run `.venv/bin/python migrate_cloudsql.py` to apply the explicit V2 evidence
   schema. This is the only schema-changing step; importing the adapter never
   connects or migrates.
5. Run `.venv/bin/python cloudsql_mirror.py --max-records 10` for a bounded
   write/readiness check. Normal `restart.sh` launches `cloudsql_mirror.py
   --loop` when both the `.venv` runtime and ignored profile exist. Inspect
   `/api/v2/cloudsql/status` or `v2_data/cloudsql_mirror_status.json`; the latter
   contains operational state only and no credential.

If Cloud SQL or Keychain is unavailable, the adapter fails closed and the
legacy CSV/JSONL/SQLite paths remain authoritative. Never add a database URL,
password, service-account key, `.env`, or `scalp.keys` to the repository.

### Read-only provider capture

- The optional Unusual Whales and IVolatility subscriptions were canceled on
  2026-08-29. Both restart paths explicitly set their capture flags to `0`.
- Historical provider evidence and adapters remain intact, but no new UW flow
  polls or IVolatility EOD chain requests are expected.
- `/api/v2/institutional-flow/status` and
  `/api/v2/options-intelligence/status` show configuration, store counts, and
  the most recent capture result. Neither endpoint starts a broker action.
- Provider workers are intentionally separate from Guard/order polling. Missing,
  stale, rate-limited, or forbidden data remains explicit and cannot be changed
  into a neutral or positive trade input.

- Automated Standard callers and Wave shadow positions remain limited to SPY
  options with 0–2 calendar DTE by the unchanged default validator. The explicit
  dashboard manual-paper endpoint additionally accepts Alpaca-validated,
  active/tradable US-equity or ETF options with 0–60 DTE under
  `scalpr-manual-wide-options-0-60dte-v2`. Stock and index-option trades remain
  blocked. Non-SPY or >2-DTE manual positions keep the unchanged Guard ratchet,
  are labeled `manual_out_of_envelope`, and are excluded from validated metrics,
  entry enrichment, incubation, and frozen cohorts. See ADR-019.
- `tick_log.csv` rotates at 32 MiB into hashed gzip files under ignored `tick_logs/`. Rotation retains the CSV header and never deletes historical rows.
- `/api/storage/health` reports disk headroom and tracked evidence sizes without loading logs into memory.
- `runtime_health_v1.jsonl` records bounded feed, Guard quote, reconciliation, storage, and execution transitions. Missing decision inputs remain explicit in their originating records.

## Paper climb-add ladder

- `scalpr-climb-add-paper-v0` is off unless the server starts with
  `SCALPR_CLIMB_ADDS_ENABLED=1` and the New Trade form is opted in.
- It is physically blocked under `--live` and remains limited to SPY options
  with 0-2 DTE.
- The default policy adds one contract at a time, at most twice, after Wave's
  synchronized ATR/VWAP/profit/confirmation gates pass. It never averages down.
- Adds use a price-capped limit order. Alpaca quantity and weighted entry are
  read before and after the order; the Guard then protects the reconciled total
  quantity without restarting its grace period.
- Decisions append to ignored `climb_add_events_v0.jsonl`; fills/rejections also
  appear in `runtime_health_v1.jsonl`.
- Do not restart to activate it while any paper position is open, because Guard
  state is not persistent.

## SPX Wave shadow simulation

- SPX/SPXW is enabled only in Wave shadow scope v5 and is tagged into Cohort E.
  It cannot submit broker orders and does not expand Standard-mode trading.
- `/api/wave/index-capability` performs a read-only entitlement and data-density
  check. `available: false` is a hard block; do not substitute SPY.
- The underlying source is Alpaca's native `/v1beta1/indices/*/values` API.
  Timestamp/value points are incrementally cached and aggregated to minute bars
  for `intraday-atr-v0`.
- SPX has no exchange volume. Its directional alignment reference is
  `sampled_index_twap`; records must never label this as VWAP.
- SPX and SPXW option roots canonicalize to the SPX underlying. Contracts must
  be 0-60 DTE, match CALL/PUT direction, and exist in the latest cached SPX
  Workup. NDX/NDXP, RUT, and VIX remain blocked.
- A 403/429, missing provider timestamp, insufficient ATR warm-up, stale option
  bid, or underlying/option timestamp skew blocks creation or observation.
