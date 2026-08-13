# Feed Qualification Gate v1 — Spec (Rev 4, for Codex)

**Status: implementation contract candidate. Read-only, flag-gated (default off), append-only. No Guard/order/execution authority.** Build after the contract-data fix deploys; Stage 3 (cohort lock) stays a separate explicit approval, blocked until this gate is green and true-free disk ≥15% at lock.

**Terminology:** SIP = consolidated NBBO. IEX = single-venue quote (cross-check only).

**Rev 4 changelog (4 gaps + activation):**
1. **Canary selection is provably non-circular** — every selection input must have `observed_at < hold_interval_start`; no current-poll quote or snapshot greek may pick that interval. OPRA freshness defined = ≤5 s; the OPRA gate also **enforces the approved 6% spread**, so it's a true executability check. (§1)
2. **Qualification-epoch activation record** — only sessions beginning after `activated_at_utc` count; when >5 CLEAN, the lock pins the **earliest five** CLEAN sessions in the epoch. (§Activation)
3. **IEX aggregation defined** — per-feed sub-verdicts; SIP PASS + IEX INSUFFICIENT ⇒ PASS; SIP PASS + adequately-sampled IEX FAIL ⇒ FAIL. (§2)
4. **Account-flat proof bypasses the cache** — uncached read-only broker read, paper identity, 0 positions, 0 open orders, max proof age, fail-closed on error. (§6)

## 0. Frozen authorities & definitions
- **Calendar:** add **`exchange_calendars` `XNYS`** as a pinned dependency **installed and tested in the actual runtime launched by `restart.sh` (system Python 3.14.6 — not a `.venv`)**; stamp the exact version in `config_hash`. Defines trading days, holidays, early closes, session bounds; tz America/New_York; premarket 04:00–09:30 ET. *(Risk: verify `exchange_calendars`/pandas/numpy import on 3.14.6 before threshold approval — 3.14 wheels may be unavailable and may force a Python pin.)*
- **Scheduled buckets** derived deterministically from that calendar per date (incl. half-days).
- **Halts (v1):** no exclusion — a missing sample is always MISSING; the feed is never credited for an unproven halt.
- **Granularity + aggregation:** compare feeds at **1-minute** shared-UTC bars (divergence + correlation); build **5-minute** buckets with a **new deterministic aggregator** (`feed_qual_agg_1m_to_5m`, half-open/watermark — *not* `bar_builder`, which emits 10-s bars). A 5-min bucket is **VALID only if all five scheduled 1-min constituents are present**; else MISSING.
- **Feed roles:** SIP = completeness/quality authority; IEX = shared-timestamp cross-check, valid only when `≥ iex_min_shared_samples`.

## 1. Live collector (`feed_qual_collector_v1`) + OPRA canary
Read-only, flag-gated (`FEED_QUAL_COLLECTOR_ENABLED`, default off), premarket + RTH. SIP + IEX 1-min bars + underlying NBBO. **OPRA canary sampler:**
- **Non-circular selection (Rev 4):** the canary contract for a hold interval is chosen using **only inputs with `observed_at < hold_interval_start`** — OCC metadata, delta, and prior-interval chain liquidity (volume/OI). **No current-poll quote and no snapshot greek gated on a fresh current quote may influence selection** (the assembler's "vendor delta only if current quote fresh" path must not feed selection). The contract is held for `hold_interval_minutes`; then **this** interval's quotes are measured against the gate.
- **Every scheduled poll is counted in the denominator** — including selection failures and missing/stale/crossed quotes.
- **OPRA freshness ≔ quote age ≤ 5 s.**

## 2. Gates (a session is CLEAN only if all aggregate verdicts PASS)
- **Bar gate (SIP authority):** valid 5-min completeness (all-5), duplicates, NaN/zero; SIP↔IEX 1-min divergence + correlation on shared minutes.
- **Quote gate (NBBO = SIP):** freshness ≤5s, two-sided validity, **crossed rate** (strict) and **locked rate** (looser, separate), coverage, max gap.
- **Premarket gate:** SIP completeness (primary) + IEX shared-timestamp cross-check.
- **OPRA / executability gate (HARD):** per canary poll, **fresh (≤5s) + two-sided + not crossed + spread ≤ 6%**; pass requires rate ≥ threshold **and** poll count ≥ the calendar-adjusted minimum, else `INDETERMINATE` → INCOMPLETE.
- **Direction health (reported, post-warmup only).**

**SIP/IEX aggregation (bar + premarket) — Issue 3:** store `sip_subverdict`, `iex_subverdict`, `iex_shared_samples`, `aggregate`:
```
SIP FAIL                                   -> FAIL
SIP PASS & IEX FAIL (iex_shared >= min)    -> FAIL
SIP PASS & IEX PASS                        -> PASS
SIP PASS & IEX INSUFFICIENT (iex_shared<min)-> PASS
```

## 3. Frozen thresholds (operator `CONFIRM` after Codex pins the calendar version on the real runtime)
```yaml
feed_qual_gate:
  version: feed-qual-gate-v1
  calendar: { source: exchange_calendars, market: XNYS, version: <PIN installed on system py3.14.6> }
  timezone: America/New_York
  premarket_window_et: ["04:00","09:30"]
  halt_policy: none_all_gaps_missing
  warmup_minutes: 70
  iex_min_shared_samples: 30
  bar_gate: { sip_completeness_min: 0.98, duplicates_max: 0, nan_or_zero_bars_max: 0,
              sip_iex_median_divergence_bps_max: 2, sip_iex_max_divergence_bps_max: 10,
              sip_iex_aligned_correlation_min: 0.99 }
  quote_gate: { freshness_min: 0.95, two_sided_min: 0.98, crossed_rate_max: 0.005,
                locked_rate_max: 0.05, coverage_min: 0.98, max_gap_seconds_max: 15 }
  premarket_gate: { sip_completeness_min: 0.95, sip_iex_max_divergence_bps_max: 10 }
  opra_gate: { canary_cadence_seconds: 60, hold_interval_minutes: 5, freshness_seconds_max: 5,
               fresh_two_sided_min: 0.90, crossed_rate_max: 0.005, spread_pct_max: 6.0,
               min_polls_fraction_of_scheduled_rth_minutes: 0.90 }
  direction_health_post_warmup_fresh_min: 0.95   # reported only
  session_min_rth_coverage_for_clean: { numerator: valid_5min_buckets_present,
                                        denominator: scheduled_5min_buckets_from_calendar, min: 0.95 }
  qualification: { clean_sessions_required: 5, pin_rule: earliest_5_clean_after_activation }
  account_flat_proof: { uncached: true, paper_identity_required: true, positions_max: 0,
                        open_orders_max: 0, max_proof_age_seconds: 30, fail_closed_on_error: true }
  disk: { disk_headroom_min: 0.15, measurement: "shutil.disk_usage(workspace_volume).free/total",
          evaluated_at_lock: true }
```

## Activation record (qualification epoch — Issue 2)
Before any session can count, write one append-only activation record:
```
{ qualification_epoch_id, activated_at_utc, feed_qual_version, threshold_hash,
  calendar {source, market, version}, impl_hashes {collector, agg, gate, calendar} }
```
- **Only sessions whose start `> activated_at_utc` may count.**
- Changing any threshold/calendar ⇒ new `feed_qual_version` ⇒ **new `qualification_epoch_id` + `activated_at_utc`** (fresh prospective clock; old sessions never re-judged).
- **Pinning:** when >5 CLEAN sessions exist in the epoch, the lock pins the **earliest five** CLEAN sessions after activation (deterministic).

## 4. Sessions record (`feed_quality_sessions_v1.jsonl`)
Append-only, with `qualification_epoch_id`, `session_record_id, revision, supersedes_record_id`, `feed_qual_version, config_hash, threshold_hash`, `impl_hashes`, `sealed_raw_partition_hashes`, per-axis `evidence_counts`, per-gate `{sip_subverdict, iex_subverdict, aggregate}` and `opra_verdict`, `direction_health`, `classification ∈ CLEAN|DIAGNOSTIC|INCOMPLETE`. Corrections create a new revision superseding the prior. CLEAN = bar+quote+premarket+opra aggregates PASS and RTH coverage ≥ floor.

## 5. Qualification
Count CLEAN sessions in the current epoch, one canonical record per session_date (highest non-superseded revision), scope SPY; require ≥5; pin the earliest five. DIAGNOSTIC/INCOMPLETE never count.

## 6. Fail-closed lock wiring (`entry_cohort_lock_v1.py`)
Refuse to lock unless all hold, and stamp the evidence into the lock record:
- the **exact five canonical `session_record_id`s + content hashes** from the current epoch; any counted date with a fork/ambiguous supersession ⇒ **fail closed**;
- **disk** `shutil.disk_usage(...).free/total ≥ 0.15` at lock time (value + timestamp stamped);
- **account-flat proof (Rev 4):** an **uncached** read-only broker read (NOT `/api/holdings`, which caches 2.5 s) verifying **paper-account identity, positions == 0, open_orders == 0**, `proof_age ≤ max_proof_age_seconds`, **fail closed on any query error**; read-only, no order authority; stamp `{account_kind: paper, positions, open_orders, proof_utc}`;
- existing preconditions (thresholds confirmed, impl hashes, `target_session`, pre-open).
No override path.

## 7. Dashboard
Read-only `X/5 clean sessions` (current epoch), per-axis latest verdicts (incl. OPRA + SIP/IEX sub-verdicts), classification, true-free disk %. No effect on capture/trading/Guard/orders.

## Activation prerequisites (operational, before the first qualifying session)
1. **Install + pin `exchange_calendars` in the runtime `restart.sh` actually launches (system Python 3.14.6)** and confirm `import exchange_calendars` + an `XNYS` schedule works there; stamp the version. Ensure tests run in that same interpreter (not a `.venv`). *(3.14 wheel availability is a live risk — resolve first.)*
2. **True-free disk ≥ 15%** by `shutil.disk_usage` (currently 14.95%, ~119 MB short — free several GB for margin).
3. Contract-data fix deployed; **qualification epoch activated pre-session.**

## Tests
UTC alignment; calendar holiday/early-close/DST denominators; **non-circular canary** (selection uses only `observed_at < hold_interval_start`; a session of all-stale/crossed OPRA still records full poll count and **fails** the OPRA gate); OPRA spread ≤6% enforced; 5-min all-5 validity; halt gaps stay MISSING; **SIP/IEX aggregation matrix** (incl. adequately-sampled IEX FAIL ⇒ FAIL, INSUFFICIENT ⇒ PASS); crossed vs locked separated; **activation epoch** (pre-activation session excluded; earliest-5 pinning; threshold change starts new epoch); revisioning one-canonical-per-date, fork ⇒ lock fails closed; **account-flat proof** uncached, paper identity, 0 positions/0 open orders, max age, fail-closed on error, read-only (no order imports); disk <15% ⇒ lock refuses; isolation (no broker/Guard/order imports in collector/gate; flag-off ⇒ zero collection).

## Non-goals
No Guard/order/execution authority; no live trading; observation-only. No threshold loosening. Mechanical PASS/FAIL only — not a score or probability.
