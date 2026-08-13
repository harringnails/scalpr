# Wave Riding — Design Return (`wave-riding-v0`, shadow-only)

**Status: DESIGN ONLY. No implementation code written yet.** This document is the
pre-implementation return requested in §30: it maps Wave Riding onto the existing
codebase, proposes the module split, and specifies the state machine, config,
formulas, idempotency, restart recovery, Guard interaction, and test plan — then
surfaces the real conflicts that need a decision before code is written.

Guiding principle (verbatim intent): *Wave Riding may increase exposure only
after the market confirms the existing position, and it must stop adding before
the strategy exceeds its predetermined capital and risk limits.* Anti-martingale
only — it adds to winners, never averages down. `allow_averaging_down = false`,
non-configurable. Shadow-only: no live add or exit orders in v0.

---

## 1. Existing modules that can be reused

The Phase 1 + ratchet work already gives Wave Riding most of its plumbing. Reuse
is READ-ONLY — Wave Riding does not change any existing semantics (§30).

| Existing asset | Reused for | Notes |
|---|---|---|
| `feature_engine._atomic_append` / `_iter_jsonl` | Crash-safe append + tolerant reads for the audit/order/position logs | Same durability guarantees as the label store |
| `feature_engine.canonical_hash` | Order idempotency keys + observation hashes | Deterministic, sorted-key SHA-256 |
| `feature_engine.assess_quote_quality` + two-tier staleness + `quote_bucket` | Quote gate for every add/peak/exit evaluation (crossed/missing/unusable → block; >15m warn; >60m materially stale) | Already the exact "quote valid & fresh" logic §6/§13 needs |
| `feature_engine._proxy_option_price` | Shadow simulated-fill pricing for adds/exits when only underlying moves are known | Clearly proxy; realized bid/ask preferred when available |
| `label_lifecycle` idempotency + versioned-correction + `ERROR_RETRYABLE` + append-only pattern | Blueprint for the order-lock / dedup / restart-safe state store | Reuse the *pattern*, not the label records |
| `session_snapshot._atomic_write_json` + cross-process lock + `calendar_verified` | Durable WavePosition record (temp+fsync+rename); market-calendar-aware windows | Whole-file record ⇒ atomic replace |
| `entry_policy._intraday_context` / `_session_bars` | VWAP, opening range, session bars for underlying confirmation + intraday ATR | IEX-degraded (see conflict F) |
| `premarket._true_atr` / `_ohlc_status` | ATR source (see conflict B — daily vs intraday) | |
| `workup` greeks / bid / ask / `spread_pct` / `tradeable` | Option executable value, spread gate, delta band | |
| `scalp_server.Guard` | Reference for giveback/confirm-ticks/grace patterns; **not modified** | Wave Riding uses a *different value basis* (executable bid $, whole position) |
| `Platform._wait_fill`, `_precheck_snapshot`, `_journal`, `DataFeed` handling, the shadow-loop scheduler | Live adapter (later stages) + shadow observation hook | v0 only adds a shadow tick |
| `intel_validation_report` pattern | Baseline-comparison + promotion-metrics report | |

## 2. Proposed new modules and responsibilities

All new, all isolated behind the feature flag; nothing imports into Standard mode.

| New module | Responsibility |
|---|---|
| `wave_riding.py` | The direction-normalized decision **engine** + explicit **state machine** + `WavePosition` record (fully simulated; carries referential-only `source_*` fields, never real-position state). Pure/testable: given a frozen observation + config + current state, returns the next state, any order intent, and the full audit record. No I/O, no broker. `WAVE_RIDING_VERSION = "wave-riding-v0"`. |
| `wave_config.py` | Versioned `WaveConfig` (dataclass) + defaults + **validation** (rejects `allow_averaging_down=true`, enforces caps are present). |
| `wave_order_adapter.py` | `OrderAdapter` interface with `ShadowOrderAdapter` (simulated fills, the only one wired in v0) and a stubbed `LiveBrokerOrderAdapter` (raises `NotImplementedError` until Stage ≥2). Identical decision code above; the adapter is the *only* live/shadow difference (§21). |
| `wave_store.py` | Durable persistence: atomic `WavePosition` record + append-only `wave_observations`, `wave_orders`, `wave_exits` logs. Restart reconstruction + reconciliation. |
| `wave_baselines.py` | Baseline A (existing whole-position ratchet on 1 contract), Baseline B (1-contract buy&hold to WR terminal ts), and the comparison table (§22). Baseline A reuses `Guard` logic in a pure sim. |
| `wave_report.py` | Shadow results + promotion metrics (§28), same reporting style as `intel_validation_report`. |
| `test_wave_riding.py` | Full unit/integration/replay/failure-recovery suite (§26). |

Server wiring (v0): one **shadow observation hook** in the existing scheduler
(no second scheduler), plus a `WAVE RIDING` UI mode selector and read-only status
panel. The initial-order path and live adapter are **not** wired in v0.

## 3. State-transition table

`WAVE_RIDING_VERSION = "wave-riding-v0"`. Every transition writes an audit record.

| From | Event / guard | To | Effect |
|---|---|---|---|
| DISABLED | user enables WR (ack exposure) | ARMED | load+validate config; freeze max-exposure |
| ARMED | initial order submitted (shadow: seeded) | INITIAL_ORDER_PENDING | record intended initial qty |
| INITIAL_ORDER_PENDING | fill confirmed (shadow: simulated fill) | INITIAL_POSITION_OPEN | set entry, anchor=entry underlying, peak=exec bid value |
| INITIAL_POSITION_OPEN | — | MONITORING_WAVE | begin observation |
| MONITORING_WAVE | all 17 add conditions first true | ADD_CONFIRMING | start confirmation timer |
| ADD_CONFIRMING | any required condition fails | MONITORING_WAVE | **reset** confirmation timer |
| ADD_CONFIRMING | persistence elapsed **and** risk authorizes | ADD_PENDING | acquire order lock; emit keyed add intent |
| ADD_PENDING | full fill (shadow: sim fill) | ADD_FILLED | — |
| ADD_PENDING | partial fill | ADD_PARTIALLY_FILLED | trust broker qty |
| ADD_PENDING | reject / timeout | MONITORING_WAVE or SUSPENDED | **do not** update qty/anchor/count |
| ADD_FILLED | post-fill bookkeeping | COOLDOWN | update anchor, qty, weighted cost, peak, count |
| COOLDOWN | cooldown elapsed | MONITORING_WAVE | rearm adds |
| MONITORING_WAVE / COOLDOWN | max adds / contracts / cost / risk reached | MAX_POSITION_REACHED | stop seeking adds; keep managing exit |
| any active | valid reversal or higher-priority exit | REVERSAL_TRIGGERED | cancel pending add first |
| REVERSAL_TRIGGERED | liquidation submitted (shadow: sim) | LIQUIDATING | sell whole broker-confirmed qty |
| LIQUIDATING | partial fill | PARTIALLY_LIQUIDATED | continue until zero |
| LIQUIDATING/PARTIALLY_LIQUIDATED | broker-confirmed qty == 0 | CLOSED | finalize P&L |
| any | data outage > threshold | SUSPENDED | block adds; escalate; keep exit active |
| any | unrecoverable error | ERROR | freeze; require manual review |

## 4. Configuration schema (versioned)

Exactly the §20 schema, as a validated `WaveConfig` (subset shown; full schema in
code will carry types + bounds). Key invariants enforced at load:
`allow_averaging_down` must be `false`; `shadow_mode` and
`live_add_orders_enabled=false` in v0; all caps (`max_adds`,
`max_total_contracts`, `max_total_cost_usd`, `max_position_risk_usd`) must be
present and finite. Trigger config is versioned (`add_trigger.type` ∈
`ABSOLUTE_POINTS | PERCENTAGE | ATR_FRACTION`; v0 = `ATR_FRACTION`, `0.40`). ATR
sub-config (`intraday-atr-v0`): `intraday_atr_timeframe_min=5`,
`intraday_atr_period=14`, `atr_bars_source="regular_session"` (premarket only if
explicitly enabled), `atr_warmup_min_bars=5`, `atr_full_bars=14`.
Defaults: initial 1, add 1, max_adds 2, max_total 3, confirm 10s, cooldown 30s,
giveback 2.5%, spread_noise_multiplier 1.5, max_spread 5%, max_quote_age 5s,
profit-funded on (ratio 0.50), open delay 5m, no-adds-before-close 15m.

## 5. Exact add-trigger formula

**DECISION (confirmed): the wave distance is measured against a frozen INTRADAY
ATR computed from 5-minute bars of the UNDERLYING — a new versioned measure, NOT
`premarket._true_atr` (which is daily).** Rationale: daily ATR is too large for
intraday pyramiding (0.40× rarely fires), 1-minute ATR is too noisy for options
(spread/spike-driven false signals); 5-minute balances responsiveness and
stability. ATR is computed on the *underlying* (clean of delta/gamma/IV/theta/
spread); the option's executable bid separately confirms the position benefits.

Versioned intraday-ATR definition (`intraday-atr-v0`):

```
intraday_atr_timeframe   = 5 minutes
intraday_atr_period      = 14 completed bars           # exclude the forming bar
add_trigger_atr_fraction = 0.40                         # research start, not optimized
bars source              = REGULAR-SESSION bars only    # premarket bars only if config enables
```

Direction-normalized trigger (one engine), measured from the LATEST confirmed
wave anchor against the ATR **frozen when the current wave began**:

```
direction_sign        = +1 if side == CALL else -1
directional_move      = (current_underlying_price - last_wave_anchor_underlying_price) * direction_sign
required_move         = add_trigger_atr_fraction * frozen_wave_atr        # goalpost is FIXED per wave
underlying_triggered  = directional_move >= required_move

option_exec_now       = current_bid                                       # executable, NOT last/mid
option_gain_pct       = (option_exec_now - last_add_exec_bid) / last_add_exec_bid * 100
option_confirmed      = (option_exec_now > last_add_exec_bid)
                        and (option_gain_pct >= minimum_option_gain_for_add_pct)
```

**Frozen-goalpost rule.** `frozen_wave_atr` is captured at wave start and is NOT
recomputed from a changing ATR during the same wave — expanding volatility must
never move the goalpost after the trade has advanced. On each confirmed simulated
add: (1) update the wave anchor to the underlying price at the simulated fill,
(2) compute and **freeze a new 5-minute ATR** for the next wave, (3) start a new
confirmation cycle.

**ATR warm-up policy** (explicit — never silently substitute daily ATR):

```
< 5 completed 5-min bars   → ADD_BLOCKED_ATR_WARMUP (no add)
5–13 completed bars        → ATR from available bars, quality = PARTIAL_WARMUP
                             (optionally require a higher confirmation threshold)
≥ 14 completed bars        → quality = FULL
```

An add is **eligible** only when ALL 17 §5 conditions hold — including
`position_profitable` (whole position on executable bid), VWAP/trend alignment
for the side, quote fresh + `spread_pct <= max_spread_pct`, confirmation
persistence satisfied, cooldown elapsed, no cap breached, no pending add, market
open and outside the open/close windows, ATR not in warm-up block, and no hard
veto. Any failure ⇒ no order, and every failed evaluation records its blocking
reason codes.

**Per-evaluation ATR audit fields** (added to the observation record, §23):
`atr_value`, `atr_period_used`, `atr_bar_count`, `atr_timeframe`, `atr_quality`
(FULL / PARTIAL_WARMUP / warmup-blocked), `atr_as_of`, `frozen_wave_atr`,
`required_directional_move`, `actual_directional_move`. All ATR parameters are
configurable and versioned.

## 6. Exact peak and reversal formula

Executable value of the WHOLE accumulated position, on the **bid** (never last):

```
current_executable_value = current_bid * open_quantity * 100

# peak updates only on a VALID quote (fresh, market open, bid>0, not crossed,
# quality ok/locked but NOT materially stale). Optional N-observation peak
# confirmation (config, default off) so one anomalous bid can't set a false peak.
if quote_valid and current_executable_value > peak_executable_value:
    peak_executable_value = current_executable_value    # (after optional N-persist)

giveback_from_peak_pct = (peak_executable_value - current_executable_value)
                         / peak_executable_value * 100

effective_giveback_pct = max(configured_giveback_pct,
                             spread_noise_multiplier * current_spread_pct)
# volatility_noise_floor_pct is STUBBED/disabled until a valid method exists —
# not fabricated (consistent with RATCHET_NOISE_ASSESSMENT).

EXIT when giveback_from_peak_pct >= effective_giveback_pct   # inclusive rule (documented)
  -> sell qty_to_sell = current_open_quantity (whole, broker-confirmed at submit)
```

This deliberately uses the **bid** (executable) — the exact mitigation #1 flagged
in `RATCHET_NOISE_ASSESSMENT.md`, and the reason Wave Riding's basis differs from
the current Guard (which measures % off entry on the mid).

## 7. Order idempotency design

Mirrors the label-lifecycle idempotency discipline. Every order intent (add or
exit), shadow or live, carries:

```
idempotency_key = canonical_hash(position_id, wave_sequence, contract_symbol,
                                 intended_quantity, signal_timestamp, strategy_version)
```

- One open add at a time: `ADD_PENDING` blocks further add submissions; an order
  lock guards concurrent ticks so duplicate ticks cannot double-submit.
- The `wave_orders` log is append-only; a write with an existing
  `idempotency_key` is a **no-op** (re-fire, restart mid-pending, replay all
  dedupe to the same order).
- A pending order must resolve (fill / partial / reject / timeout) before the
  engine rearms. Timeout/reject → no qty/anchor/count mutation.

## 8. Restart-recovery behavior

Reconstruct, never regenerate signals (same rule as the snapshot/label work):

1. Load the durable `WavePosition` record (atomic file) + append-only
   `wave_orders` / `wave_observations` (tolerant reads skip corrupt lines).
2. In live stages, reconcile against **broker-confirmed** open quantity; in
   shadow, against the simulated open qty in the record.
3. On any quantity mismatch (manual position change, unknown fill): → SUSPENDED,
   require explicit **rearm** (§25).
4. **Never add immediately on restart.** Re-enter MONITORING_WAVE with a fresh
   confirmation interval (reset the persistence timer); a pending order at crash
   time is resolved by its idempotency key, not re-sent.
5. Data-outage-at-restart → SUSPENDED until fresh, valid quotes resume.

## 9. Interaction with existing Guard and ratchet logic

This is the most important integration point, and it is where v0 stays
deliberately non-invasive.

- **Value basis differs and must not be blended.** The existing `Guard` measures
  profit as `% off entry` on the option **mid** and sells `qty=guard.qty` whole.
  Wave Riding measures the **executable-bid dollar value of the whole accumulated
  position** and manages its own peak/giveback. Two independent giveback engines
  must never compete without explicit precedence (§15).
- **Precedence (highest first):** emergency kill switch → broker/account risk
  breach → hard max-loss stop → manual full liquidation → market-data-safety exit
  → Wave Riding reversal → stall/time exit.
- **v0 (shadow) rule — CONFIRMED (D + E resolved).** Wave Riding shadow mode
  creates and OWNS an **independent, fully simulated position** seeded from real
  decision-time quotes. It simulates the initial 1-contract entry, all
  momentum-based additions, its own accumulated-position peak, its own reversal
  exit, and the full end-to-end result. It does NOT place orders, NOT read or
  mutate the live `Guard`'s quantity / average cost / peak / ratchet state / exit
  outcome, and NOT suspend or modify the live Standard ratchet. The real Guard
  keeps managing any real user position normally and completely unchanged.
- **Referential-only linkage.** Any tie to a real trade is reference metadata
  only — never shared state:

  ```
  source_decision_id · source_position_id · source_contract_symbol · source_activation_timestamp
  ```

  Wave Riding never controls or mutates that real position.
- **Simulation-only ratchet suspension.** Inside the *simulated Wave Riding
  track*, the standard dynamic ratchet is treated as suspended so WR owns the
  add/reversal behavior. This is purely a simulation rule; live Standard mode is
  unaffected. Actual live suspension is deferred to live Stage ≥4 and is out of
  scope for v0.
- **Three independent simulated tracks** from the SAME contract, timestamp, and
  initial-fill assumption (§22):
  `Baseline A` = one simulated contract under the existing ratchet rules replayed
  in simulation (reuses the rules, never calls/alters the live Guard);
  `Baseline B` = one simulated contract held to the WR terminal timestamp;
  `Wave Riding` = one simulated contract + simulated adds + WR reversal exit.
- **Storage isolation.** `Platform.guards` is keyed by symbol and holds a single
  live `Guard`. Wave Riding's simulated multi-add position lives in a separate
  `WavePosition` (in `wave_store`), keyed by `position_id`, so it never collides
  with — or reads from — the single-Guard model.

## 10. Test and replay plan

- **Unit (direction-normalized, call+put symmetry):** below-trigger → no add;
  one-tick cross → no add; persisted cross → eligible; condition fails mid-window
  → timer resets; successful add updates qty/cost/anchor/cooldown; post-cooldown
  continuation → 2nd add; reverse from peak → whole-qty exit. Every put test
  mirrors a call test with signs flipped through the one engine.
- **No averaging down:** move against, cheaper option, or unprofitable-whole →
  no add.
- **Limits:** max adds / max contracts / projected-cost cap / profit-funded fail
  → blocked; projected fill uses `ask + slippage_allowance`; budget never
  auto-raised.
- **Quote protection:** stale (two-tier), crossed, missing bid, spread over max,
  last-trade spike without bid+underlying confirmation → blocked/no false peak.
- **Duplicate protection:** concurrent ticks, repeat signal during pending,
  restart while pending → exactly one order (idempotency key).
- **Exit handling:** below/at (inclusive rule)/above effective threshold;
  pending add at exit → cancel then liquidate; partial liquidation → continue to
  zero.
- **Isolation:** WR disabled → Standard unchanged; Standard ratchet tests still
  pass; hard stop active in both modes.
- **Replay:** drive the engine from recorded `tick_logs` → deterministic
  decisions reproducible from stored observations (like the label replay).
- **Failure recovery:** restart mid-pending, data outage → block→SUSPENDED,
  reject, partial, manual modification → reconcile+rearm.
- **Baselines:** every sim records Baseline A (ratchet), Baseline B (hold), and
  Wave Riding, with the full §22 metric set.

## 11. Assumptions and conflicts discovered in the current code

These need a decision before implementation:

- **A. Options are guarded off the MID today; Wave Riding needs the BID.** The
  poll loop values options on `(bid+ask)/2`. Wave Riding's peak/exit/profit use
  the executable **bid** (§6/§13) — an intentional improvement matching
  `RATCHET_NOISE_ASSESSMENT` mitigation #1. Consequence: WR's value basis differs
  from the live Guard's; we keep them separate (never blend). No change to the
  existing Guard. *Assumption: acceptable.*
- **B. ATR timeframe — RESOLVED.** New versioned **intraday** measure
  (`intraday-atr-v0`): 14-period ATR on **completed 5-minute** regular-session
  bars of the **underlying**, frozen per wave, with an explicit warm-up policy
  (< 5 bars blocks adds; 5–13 = PARTIAL_WARMUP; ≥14 = FULL). Does NOT reuse
  `premarket._true_atr`. Full spec in §5.
- **C. Single-Guard-per-symbol model.** `Platform.guards[symbol]` holds one
  `Guard`; it cannot represent a growing multi-add position. Resolved by a
  separate `WavePosition` store keyed by `position_id`. *Assumption: acceptable.*
- **D. Ratchet suspension — RESOLVED.** Shadow v0 does **not** suspend or modify
  the live Standard ratchet. Suspension exists only *inside the simulated WR
  track* (WR owns simulated add/reversal); live Standard mode is unaffected. Real
  live suspension is deferred to live Stage ≥4 (out of scope for v0).
- **E. Initial-position ownership — RESOLVED.** Fully **simulated** position: WR
  creates and owns an independent simulated position seeded from real
  decision-time quotes (initial entry + adds + peak + reversal + end-to-end
  result). It never uses the real Guard's qty / avg cost / peak / ratchet state /
  exit as its own state; any link to a real trade is referential only
  (`source_decision_id`, `source_position_id`, `source_contract_symbol`,
  `source_activation_timestamp`). Nothing touches live risk.
- **F. Feed quality.** VWAP/ATR/bid on the free IEX feed (~2.5% of volume) make
  shadow fills optimistic; shadow results must be labeled feed-degraded (like the
  proxy labels) and re-validated on SIP. *Assumption: acceptable, labeled.*
- **G. Observation cadence.** `confirmation_seconds=10` / `max_quote_age=5s` need
  a faster tick than the 120s shadow loop. WR shadow needs its own observation
  cadence aligned with the price poll. *Assumption: add a dedicated WR shadow
  tick at poll cadence.*
- **H. Reuse is read-only.** WR imports `feature_engine` helpers (atomic append,
  quote quality, proxy price, hashing) without changing their semantics, honoring
  §30 "do not alter Phase 1 feature or label semantics." *Assumption: acceptable.*

---

## 12. Final implementation plan (v0 shadow — awaiting approval)

Ordered build. Each step is independently testable; nothing wires into Standard
mode; no live orders. All new files; zero edits to `Guard`, `Platform.sell/buy`,
or the live poll loop except one additive read-only shadow hook (Step 8).

**Step 1 — `wave_config.py`.** Versioned `WaveConfig` dataclass (full §20 schema +
`intraday-atr-v0` sub-config) with `load/validate`. Hard invariants:
`allow_averaging_down` must be false; `shadow_mode=true` and
`live_add_orders_enabled=false` forced in v0; all caps present/finite.
*Tests:* rejects averaging-down, rejects missing caps, defaults load, version tag.

**Step 2 — intraday ATR (`wave_atr.py` or within engine).** `intraday-atr-v0`:
14-period ATR on completed 5-min regular-session underlying bars (exclude forming
bar); warm-up ladder (<5 → block, 5–13 → PARTIAL_WARMUP, ≥14 → FULL); `freeze()`
returns `frozen_wave_atr` + audit fields. Bars from `entry_policy._session_bars`
resampled to 5-min. *Tests:* warm-up boundaries, forming-bar exclusion, freeze
stability within a wave, re-freeze after add, regular-session-only.

**Step 3 — `wave_riding.py` engine + state machine (pure, no I/O).**
Direction-normalized add trigger (§5), 17-condition gate with reason codes,
dual underlying+option confirmation on executable **bid**, confirmation
persistence + reset, cooldown, whole-contract + cost/risk caps, profit-funded
gate, spread-adjusted reversal (§6), whole-position simulated exit. Explicit
state machine (§18) with the §3 transition table. `WavePosition` record (§19 +
`source_*` referential fields). *Tests:* the full §26 matrix — call/put symmetry
through one engine, no-averaging-down, limits, quote protection (two-tier stale/
crossed/missing/spike), duplicate protection, inclusive giveback rule, whole-qty
exit, cancel-pending-add-on-exit.

**Step 4 — `wave_order_adapter.py`.** `OrderAdapter` interface;
`ShadowOrderAdapter` (deterministic simulated fills: add at `ask+slippage`, exit
at `bid`; reuses `feature_engine._proxy_option_price` only when a direct quote is
unavailable, clearly flagged); `LiveBrokerOrderAdapter` stub raising
`NotImplementedError`. *Tests:* shadow fill determinism, idempotency-key dedupe,
one-open-order lock, reject/timeout paths leave state unmutated.

**Step 5 — `wave_store.py`.** Atomic `WavePosition` persistence
(`session_snapshot._atomic_write_json` pattern) + append-only `wave_observations`
/ `wave_orders` / `wave_exits` (`feature_engine._atomic_append` + tolerant reads).
Restart reconstruction + quantity reconciliation → SUSPENDED/rearm. *Tests:*
restart mid-pending (no duplicate via idempotency key), corrupt-line tolerance,
no-add-immediately-on-restart (fresh confirmation interval), manual-modification
mismatch → SUSPENDED.

**Step 6 — `wave_baselines.py`.** Three independent simulated tracks from one seed
(§22): Baseline A replays the existing ratchet rules in pure simulation (imports
the rule constants, never touches the live Guard), Baseline B is 1-contract hold
to the WR terminal ts, Wave Riding is the engine's own track. Emits the full §22
metric set. *Tests:* identical seed → three comparable tracks; A never calls live
Guard; metric math.

**Step 7 — `wave_report.py`.** Shadow results + §28 promotion metrics (per ticker/
delta/DTE/regime/side), same style as `intel_validation_report`. Non-qualifying,
experimental banner. *Tests:* metric aggregation on a synthetic fixture.

**Step 8 — server + UI wiring (additive, isolated).** A read-only WR shadow
observation hook at price-poll cadence (its own tick; not the 120s loop) behind
the feature flag; `STANDARD | WAVE RIDING` mode selector; read-only status panel
(§24 fields) + worst-case-exposure acknowledgement before arm. No change to
`Guard`/`Platform` order paths. *Tests:* WR-disabled → Standard byte-for-byte
unchanged (guard/ratchet suites still pass); flag gating.

**Step 9 — replay + acceptance.** Drive the engine from recorded `tick_logs` for
deterministic, reproducible-from-stored-observations decisions; verify the full
§27 shadow acceptance checklist (1–20). Update `SCALPR_OVERVIEW.md` version
register (`wave-riding-v0`, `intraday-atr-v0`) with the experimental/shadow-only
status.

**Completion gate:** §27 items 1–20 all demonstrated by tests; Standard-mode
isolation proven; no live order path reachable in v0. Promotion to paper (§28)
and live (§29) remains behind the staged approvals and is out of scope here.

---

## 13. Shadow Observer increment plan (`wave-riding-shadow-observer-v0`) — awaiting approval

Turns the offline/replay simulator into a live SHADOW-OBSERVATION system: the user
deliberately starts a fully-simulated Wave Riding position on a chosen contract,
and real-time sequential observations drive the existing pure engine + three-track
harness. Still fully simulated — never submits, cancels, or modifies any broker
order; never mutates `Guard`, `Platform`, live position/order state, or Standard
mode. `observer_version = "wave-riding-shadow-observer-v0"`.

**New / changed modules**
| Module | Change |
|---|---|
| `wave_quotes.py` (new) | READ-ONLY quote source. Builds one synchronized observation from `platform.stock_data.get_stock_latest_quote` (underlying, IEX/SIP) + `platform.option_data.get_option_latest_quote` (option, OPRA) + a rolling 5-min ATR from IEX minute bars. Records per-field source + each field's own provider timestamp + quote age + spread. Never touches guards/orders. |
| `wave_observer.py` (new) | `ShadowObserver` lifecycle: `start / pause / resume / stop`, and `observe_tick` (dedupe by observation identity → skew gate → feed engine once via `WaveRunner` → persist). Active-position registry. |
| `wave_store.py` (extend) | Persist immutable seed + `seed_hash`; active-index of live sim positions; `last_processed_observation_id`; `reload_active`. |
| `wave_baselines.py` (extend) | Add `simulated_standard_policy_version` + `simulated_standard_policy_hash` (hash of the ratchet ladder/params) to every comparison result; expose the params for hashing. |
| `wave_riding.py` (additive) | New obs flag `unsynchronized` → reason `OBSERVATION_UNSYNCHRONIZED` blocks add AND reversal (emergency/hard-stop still allowed). Defaults false, so v0 behavior is unchanged. |
| `scalp_server.py` (additive, gated) | POST `/api/wave/start|pause|resume|stop` (shadow-only) + a READ-ONLY observer tick inside the existing `_poll_loop` (gated by the flag, exception-isolated, only runs when active sim positions exist) + extend `/api/wave/status`. |
| `dashboard.html` (additive) | Start/pause/resume/stop controls + the full field display + three-track results, under the "SHADOW SIMULATION — no live orders" label. |
| `test_wave_observer.py` (new) | Full acceptance matrix + baseline-drift regression. |

**Entry flow.** `start(underlying_symbol, option_contract_symbol, direction,
source_decision_id?, source_position_id?, activation_timestamp)` →
(1) fetch ONE synchronized decision-time observation; (2) validate underlying +
option quote quality; (3) conservative initial fill = ask + slippage;
(4) compute + freeze the initial 5-min ATR; (5) seed all three tracks from the
SAME immutable observation + fill; (6) persist seed + `seed_hash`; (7) begin
observation only after persistence succeeds. Any quote/ATR failure → NO position
created, explicit blocking reason returned.

**Observation bridge (read-only).** Reuses the polling path; per observation it
captures underlying price+ts, option bid/ask/mid+ts, per-field quote source,
quote age, spread, completed 5-min ATR, session status, and an observation
sequence number. Observations feed the engine sequentially and exactly once.

**Idempotency.** Observation identity =
`hash(shadow_position_id, underlying_quote_ts, option_quote_ts, observation_sequence, observer_version)`.
The bridge skips an observation whose identity was already processed, so repeated
poll cycles cannot create duplicate transitions/fills. `last_processed_observation_id`
is persisted.

**Timing alignment.** Underlying and option quote timestamps are recorded
independently; `max_underlying_option_timestamp_skew_seconds` (config) bounds
their gap. Exceeding it flags `OBSERVATION_UNSYNCHRONIZED` and blocks add/exit
evaluation for that observation (except a higher-priority safety rule such as
emergency), auditably.

**Start/stop controls.** Shadow-only. `stop` finalizes the tracks with
`exit_reason = MANUAL_SHADOW_STOP` using the current valid simulated liquidation
quote (bid − slippage); it never touches a real position. Pause halts observation
without finalizing; resume requires a fresh synchronized quote.

**Restart recovery.** Reload active sim positions; do NOT manufacture observations
for the missing period; require a fresh synchronized quote before resuming;
reset pending confirmation persistence; preserve quantity, cost basis, peaks,
anchors, and completed adds; prevent duplicate fills from the last processed
observation (via `last_processed_observation_id`).

**Data-source documentation (per field — NOT uniformly "IEX").**
`underlying_quote`: Alpaca stock latest quote, **IEX** (free) [SIP if configured];
`option_quote (bid/ask)`: Alpaca **options** latest quote (**OPRA**, a different
source); `underlying_5m_bars`: Alpaca stock minute bars (**IEX**); `timestamps`:
each field's own provider `timestamp`, recorded separately. Simulated option
fills are labeled OPRA-derived, underlying context IEX-derived.

**Baseline-policy drift.** No Guard refactor. Every comparison result carries
`simulated_standard_policy_version` + `simulated_standard_policy_hash`; a
regression test replays a representative price path through both
`simulate_standard_ratchet` and the live `Guard` (via the repo's test stub
shims) and reports any mismatch, so baseline drift is caught.

**UI.** Panel labeled "WAVE RIDING — SHADOW SIMULATION · No live orders will be
submitted", showing active state, contract, simulated qty, simulated avg cost,
additions completed, frozen ATR, distance to next add, executable P&L, peak P&L,
current reversal threshold, latest observation age, blocked reasons, and all
three track results.

**Tests → acceptance (1–10).** create-from-valid-quote (+ blocked on bad
quote/ATR, no position) [1]; one immutable seed shared by three tracks + hash
[2]; sequential once-only feeding drives the engine [3]; grep + adapter proof no
live path [4]; isolation test — no Guard/Platform/Standard mutation, no
`scalp_server` import from wave modules [5]; duplicate-poll identity dedupe [6];
skew/stale → `OBSERVATION_UNSYNCHRONIZED` blocked + audited [7]; restart resumes
without inventing observations, requires fresh quote, no dup fill [8]; status
endpoint returns state + comparison [9]; manual stop → `MANUAL_SHADOW_STOP`
without touching a real position [10]; plus the baseline-drift regression.

**Assumptions / conflicts to confirm.**
- **Option quotes are Alpaca OPRA, underlying is IEX** — documented per-field as
  above (honoring "don't call all fills IEX"). *Assumption: acceptable.*
- **Observer tick piggybacks the existing `_poll_loop`** (gated, read-only,
  isolated) rather than a second scheduler; cadence = `POLL_SECONDS`.
  Confirmation persistence is wall-clock, so it holds regardless of cadence.
- **The engine gains a small additive `unsynchronized` gate** (no behavior change
  when the flag is absent; still `wave-riding-v0` semantics).
- **The baseline-drift regression imports the live `Guard`**, which pulls Alpaca —
  it runs under the repo's existing `/tmp/stubs` shims, like the ratchet tests.
- **A rolling IEX minute-bar buffer** feeds the 5-min ATR; IEX bar coverage
  (~2.5% of volume) makes ATR/fills optimistic and is labeled as such.

---

*No ML, no institutional-intent inference, no uncalibrated probability. Shadow
only; live add/exit orders disabled. Experimental; not authorized for live
automation. Promotion follows the staged §27→§28→§29 gates.*
