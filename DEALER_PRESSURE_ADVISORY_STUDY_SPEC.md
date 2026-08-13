# Dealer Pressure Advisory Study — Spec (for Codex)

**Status: research spec candidate. Advisory-only, non-qualifying, paper/shadow. Observational.** No module in this study imports gate/decision/Guard/order code, changes any frozen cohort/config/hash, or influences trade qualification. It is a **parallel observational study**, isolated from the running system by construction.

**Objective:** determine whether dealer-pressure information derived from SPY options activity contains **reproducible, prospective, incremental** information about short-horizon SPY price behavior — measured the same disciplined way as the reversal cohort. The study does **not** build a trading strategy, recommendation score, market scanner, contract selector, or multi-instrument model.

**Provenance:** authored from the operator's revised proposal. Governed by `SCALPR_V2_FLOW_AMENDMENTS.md` §I (non-qualifying firewall), §J (per-family sample budget + multiple-comparisons), §K (data-state honesty), and `DECISION_REPLAY_RECONCILED.md` (realized outcomes, no predicted/expected fields).

---

## Invariants (every phase)

1. **Advisory-only / non-qualifying.** Every record carries `is_qualifying: false` and `is_calibrated_probability: false`. No dealer-pressure value reaches `direction`, `quality`, `executability`, any act/no-act tier, or any gate — until and unless it clears Phase 8.
2. **No fused score, ever.** There is no `dealer_score`, no `overall_trade_score`, no weighted blend. Features are separate, inspectable axes. (§A/§I.)
3. **Estimated ≠ known.** Dealer inventory is unobservable; we only estimate *flow*. Fields are named `estimated_*` and never `dealer_position`/authoritative.
4. **Missing ≠ neutral (§K).** `FRESH`/`STALE`/`MISSING`/`UNAVAILABLE`/`UNUSABLE` and classification `UNKNOWN` stay distinct end-to-end; never defaulted to 0/neutral/side. `UNKNOWN` is a *desirable* outcome, never coerced to improve coverage.
5. **Point-in-time, no look-ahead.** Every greek/quote/underlying used for a print is the value **as of that trade's timestamp**; timestamps frozen into the record. No later snapshot backfills an earlier print.
6. **Immutable, append-only, hash-stamped.** Raw and derived records use `_atomic_append`, tolerant readers, and `canonical_hash` (sha256 over sorted compact json, default=str) — the existing repo convention.
7. **Isolated & flag-gated.** New files only; flag `dealer_pressure_study.enabled` default **off**; no import of broker/order/Guard/gate/decision modules (enforced by an import-graph test). Secret env var stays `UW_API_KEY`, never persisted.

---

## Phase 0 — Protect the current baseline (hard isolation)

Dealer-pressure development **cannot**: modify entry gates, scoring, exits, thresholds, or the frozen reversal cohort; add tuning parameters to the existing experiment; or influence Monday's dry run. Enforcement, not just intent:

- All code lives in new modules (`dealer_pressure_*`) with **no imports** of `scalp_server`, Guard, order paths, `entry_intelligence_*` decision/gate code, or cohort-lock code. Add an isolation test asserting the import graph is clean (mirrors the manual-scope isolation test).
- Stores are new, separate JSONL files. The study never writes to any existing cohort/decision/outcome store.
- Flag default-off; with the flag off, zero collection and zero side effects.

## Step 0 — UW field-availability audit (before any build)

Do **not** assume UW supplies the primitives. Produce an explicit **available-vs-unavailable field map** for SPY per-trade options data (mirrors flow-amendments §E). Confirm, per field, whether UW actually delivers it and at what freshness: `timestamp, option_symbol, expiration, strike, call_put, trade_price, contracts, bid, ask, bid_size, ask_size, underlying_price, delta, gamma, iv, trade_condition, exchange, trade_id, multileg_indicator, sweep_indicator`. Anything not confirmed present is treated as `UNAVAILABLE`, not assumed. The reconstruction (Phase 3) may only use confirmed fields.

**Design output for Step 0:** the audit must end in a committed field map with three buckets per field: `confirmed`, `missing_from_uw`, `present_but_unusable_for_point_in_time`. If `delta`/`gamma`/`underlying_price` are not point-in-time clean, they stay `UNAVAILABLE` for Phase 1-3 and can only re-enter later as advisory features after a separate data-quality gate.

### Step 0 — run log (2026-08-09): BLOCKED on provider auth

- **Result:** all 7 read-only probes (`stock_state, flow_alerts, darkpool, gex_levels, iv_rank, expiry_breakdown, term_structure`) returned `401 UNAUTHENTICATED`; controlled replay produced **0 usable events**.
- **Classification:** `PROVIDER_AUTH_FAILURE` — **not** a data finding. The failure occurred before data retrieval, so field presence and point-in-time consistency were never observable.
- **Therefore:** no Phase-1 field is `confirmed`, `missing_from_uw`, or `present_but_unusable_for_point_in_time`. Do **not** scaffold ingestion around assumed UW fields.
- **Root cause (isolated):** the local pipeline is correct — Keychain loads the token and the client sends `Authorization: Bearer <token>` (`unusual_whales_adapter.py:50`). The stored credential is a 15-char value that UW rejects; a valid UW **API-Dashboard** token (with API access active on the plan) is required. Not a machine, network, code, or endpoint-deprecation issue (a deprecated route would 404/410, not 401).
- **Unblock (operator action, off critical path):** obtain an API-enabled UW token (Settings → API Dashboard; API access is a separate product with a free trial), store it, re-run `uw_entitlement_validation.py`, and only after a `200` evaluate field presence / point-in-time / replay determinism.
- **Scope note:** this blocks only the parallel dealer-pressure study. It does **not** affect the reversal cohort or Monday's dry run.

**UPDATE (2026-08-09) — RESOLVED:** root cause was a Keychain **store** failure that left the old 15-char value in place, not the key or the code. A valid 36-char API-Dashboard token now stores and loads correctly. Re-run: all 7 probes `AVAILABLE` (HTTP 200); controlled replay `100 unique events, deterministic=True, lookahead_violations=0`; sanitized report at `v2_data/institutional_flow/validation/latest.json`. **Auth is unblocked.** Still pending (the actual Step 0 deliverable): produce the three-bucket field map from real records — specifically whether `delta`/`gamma`/`underlying_price` are point-in-time clean vs `present_but_unusable_for_point_in_time`. Auth passing ≠ fields confirmed; the field audit is the next step.

**Run-context note:** a `401` in any later run is an **environment-loading gap, not a new provider failure** — the valid 36-char token lives in Keychain (`scalpr.unusualwhales.api`) and is loaded by `load_keychain_env.sh`. Any process running `uw_entitlement_validation.py` or the field audit must have `UW_API_KEY` in its environment (source the loader first; verified working when `env_len=36`). Do not re-log such a `401` as `PROVIDER_AUTH_FAILURE`.

### Step 0 — next action for Codex

**UW auth is resolved; do not treat this as blocked.** From a shell with the credential loaded, `uw_entitlement_validation.py` returns all 7 probes `AVAILABLE` (HTTP 200), 100-event deterministic replay, 0 lookahead violations.

1. **Environment first.** Before any UW call, ensure `UW_API_KEY` is in the process environment (source `load_keychain_env.sh`; confirm `env_len=36`). If your execution context cannot read the login Keychain, the token must be supplied to that environment explicitly. A `401` means the env is missing the token, not that the provider failed.
2. **Run the Step 0 field audit** against real UW SPY per-trade records. Auth passing ≠ fields confirmed. Produce the committed three-bucket field map (`confirmed` / `missing_from_uw` / `present_but_unusable_for_point_in_time`) for the Phase-1 primitive list.
3. **Decisive finding:** are per-trade `delta` / `gamma` / `underlying_price` genuinely **point-in-time** (aligned to the trade timestamp) or a later/EOD snapshot? Determine empirically from records, not docs. If not point-in-time clean, they are `UNAVAILABLE` for Phases 1–3 and Phase 3 reconstruction reshapes accordingly.
4. **Do not scaffold ingestion** until the field map is committed.
5. **Guardrails unchanged:** advisory-only, non-qualifying, isolated (no gate/Guard/order/decision imports), no fused score, `estimated_*` naming, one pre-registered hypothesis with ≥200 non-overlapping episodes before any promotion. Parallel study only — does not touch the frozen reversal cohort or Monday's dry run, which remain the priority.

## Phase 1 — SPY-only raw collection

Scope: **SPY only.** Source: existing Unusual Whales integration wherever possible. Capture one immutable raw print record per qualifying option trade — **no trading interpretation attached at ingestion**:

```
schema_version: "dealer-pressure-raw-print-v1"
config_version, config_hash
raw_print_id                      # canonical_hash of identity
provider, raw_id (FK to raw ledger), received_at, observed_at   # UTC; point-in-time
option_symbol, expiration, strike, call_put
trade_price, contracts
bid, ask, bid_size, ask_size, quote_age_seconds
underlying_price
delta, gamma, iv                  # as-of observed_at; each with its own DataState
trade_condition, exchange, trade_id
multileg_indicator, sweep_indicator
field_states: {<field>: DataState}   # per-field FRESH/STALE/MISSING/UNAVAILABLE/UNUSABLE
is_qualifying: false, is_calibrated_probability: false
```

Raw records are immutable; no interpretation, no classification, no pressure math here.

## Phase 2 — Trade-side classification study (the FIRST real experiment)

Before any pressure math, establish whether customer side can be estimated reliably.

**Frozen classification rule (versioned).** Use a quote-based rule with explicit guards, stamped `classification_version` + `classification_hash`:

- `BUY` (customer buy) if `trade_price >= ask - band` on a fresh, two-sided, non-crossed quote.
- `SELL` if `trade_price <= bid + band`.
- `UNKNOWN` if between the bands, or quote is `STALE`/one-sided/`CROSSED`, or `quote_age_seconds` exceeds a fixed max, or spread exceeds a fixed width.
- `MULTILEG` if `multileg_indicator` set (spreads contaminate single-leg side inference).
- `CROSS` for cross/auction condition codes; `EXCLUDED` for condition codes that must not count.
- `band`, max quote age, and max spread are **fixed before running** and versioned. Every classification carries `classification_confidence`.

**Frozen starting default.** Unless Step 0 proves a narrower guard is required, the initial implementation should use the most conservative rule that still classifies clean prints: only fresh, two-sided, non-crossed quotes are eligible; any stale, one-sided, crossed, or wide print is `UNKNOWN`. The band, freshness ceiling, and spread ceiling belong in a hash-stamped config artifact and are not to be tuned after outcomes are visible.

**UNKNOWN is valid and desirable** — never forced to a side to raise coverage.

**Measure, at minimum:** % confidently classified; % ambiguous; bid/ask classification stability; quote-age distribution; spread-width distribution; multi-leg contamination rate; condition-code contamination rate; and the **sensitivity of the cumulative curve to uncertain prints**.

**Error-sensitivity test (primary Phase-2 gate).** Recompute the cumulative pressure curve under alternative plausible classifications and check whether direction and major inflections survive:

```
base            # the frozen rule
optimistic      # ambiguous -> favorable side
conservative    # ambiguous -> unfavorable side
unknowns_excluded
unknowns_random
unknowns_adversarial   # ambiguous -> worst-case for the apparent signal
```

**Gate:** if the curve's sign and major inflections **do not survive** small perturbations (especially `adversarial` and `unknowns_excluded`), the primitive is too fragile to trust and the study **stops here** — a successful, cheap negative result. Reconstruction (Phase 3) proceeds only if the curve is perturbation-robust.

## Phase 3 — Reconstructed dealer pressure (only after Phase 2 passes)

For a qualified, side-classified print:

```
customer_delta_i = side_sign_i * contracts_i * contract_multiplier * delta_i
estimated_dealer_delta_i = - customer_delta_i
```

`side_sign` = +1 BUY / −1 SELL; `UNKNOWN/MULTILEG/CROSS/EXCLUDED` prints do **not** contribute to the primary curve (they are recorded, and appear only in the sensitivity variants). Maintain **decomposed** cumulative values, never a single blended number:

```
schema_version: "dealer-pressure-estimate-v1"
call_pressure, put_pressure, net_pressure   # named estimated_dealer_delta_flow, NOT dealer_position
classification_version, classification_hash
coverage: {classified, unknown, excluded, multileg}
field_states, is_qualifying: false, is_calibrated_probability: false
```

Absolute dealer inventory is **estimated flow, not known position** — enforced by field naming and a schema note. The level is non-authoritative; the *change* is the object of study (per the operator's Section 1 instinct).

## Phase 4 — Advisory features (each its own axis)

Derive observational features, **each a separate inspectable axis** — explicitly **no** `dealer_score`, **no** `overall_trade_score`:

```
net_pressure_change_1m / _5m / _15m
pressure_velocity, pressure_acceleration
call_pressure_change, put_pressure_change
classification_coverage, classification_confidence
pressure_session_percentile              # normalized vs the instrument's own history
# later, still separate axes: gamma_sensitivity, pressure_price_divergence
```

Normalization (percentile / z-score vs SPY's own history) is allowed and encouraged for comparability, but each feature stays its own axis with its own `DataState`.

## Phase 5 — Pre-register ONE hypothesis (immutable artifact)

Before any outcome is examined, write **one** append-only, hash-stamped **pre-registration record** — the analogue of a cohort lock. This is the enforcement mechanism for "no adding `RSI<45` after looking," not a promise:

```
schema_version: "dealer-pressure-preregistration-v1"
prereg_id, prereg_hash, created_at_utc          # must predate the first outcome row
instrument: "SPY"
feature: <one defined dealer-pressure variable>
threshold: <fixed>                              # e.g. a historical percentile crossing
conditioning_variables: [<fixed set>]           # e.g. below-VWAP; frozen, may be empty
primary_horizon_minutes: <one>                  # the sole inferential endpoint
secondary_horizons_minutes: [...]               # recorded, explicitly NON-inferential
null: "session/time-block + regime matched, sign-flip permutation, +1 finite-sample"
episode_identity: <fields>                       # see below (non-overlap)
min_qualifying_episodes: 200
success_criteria: <defined before outcomes>
walk_forward: <fixed scheme>
classification_version, classification_hash, feature_version, config_hash
is_qualifying: false
```

**Non-overlapping episode identity (critical for honest n).** A continuous pressure curve sampled each minute yields thousands of *correlated* observations; the effective sample is far smaller. Define an episode key (instrument, signal-trigger bucket, session date) and a cooldown so the same move is counted **once**. Overlapping/duplicate triggers are recorded but do **not** count toward the 200. (§J: ≥200 **non-overlapping** episodes per frozen family; any unlisted condition is a *future* cohort, never mined from this data.)

**Episode identity default.** Use `episode_key = instrument + contract + trigger_bucket + session_date + primary_horizon_minutes`, and enforce a cooldown until either:
- the primary horizon has fully elapsed, or
- the signal returns to neutral and stays neutral for the configured reset window,

whichever is longer. That makes the 200-count a count of distinct moves, not dense sampling of one move.

## Phase 6 — Decision Replay records realized outcomes, not predictions

For each qualifying episode, store the market state at trigger time. Prediction fields are **reserved null** (per `DECISION_REPLAY_RECONCILED.md`):

```
predicted_direction = null, predicted_return = null, predicted_mfe = null
expected_value = null, confidence_score = null      # RESERVE; is_calibrated_probability: false
```

Record only **realized** results:

```
forward_return_5m / _15m / _30m
mfe_5m, mae_5m, mfe_15m, mae_15m, mfe_30m, mae_30m
```

The pre-registered **primary horizon is the inferential endpoint**. Secondary horizons are recorded for future research and **can never retroactively become the winning hypothesis** because one looked better — that would be a new pre-registration (new cohort id).

## Phase 7 — Test against the matched null (incremental information)

The question is **not** "did SPY fall after negative pressure?" — it is "did SPY fall **more often or by more** than comparable SPY periods in the same regime?" If SPY is already selling off at 10:15, pressure may merely *describe* price. The study must establish **incremental** information.

- **Matched null:** contiguous session/time-blocks receive a shared random sign (preserves within-session dependence), **matched on session-time bucket and contemporaneous market regime** so the comparison isolates incremental info rather than "price was already moving." One-sided p-value with the finite-sample `+1` correction (reuse the `entry-episode-session-block-sign-null-v1` machinery pattern; do not reuse the reversal function directly).
- **Matched null defaults:** match on `session_time_bucket`, `SPY_regime_bucket`, and `realized_volatility_bucket`; keep the sign-flip within contiguous session blocks; hold `primary_horizon_minutes` fixed; do not match on the study feature itself or any post-trigger outcome.
- Compare `P(Return_primary < 0 | signal)` vs `P(Return_primary < 0 | matched-null)`, and the **distributions** of forward return, MFE, MAE, variance, and tail behavior.
- Then repeat **prospectively through walk-forward** on an untouched window.

## Phase 8 — Promotion gate

Dealer pressure stays `ADVISORY_ONLY` until its dedicated frozen experiment clears the pre-registered evidence standard (≥200 non-overlapping episodes + realistic controls + matched-null cleared + walk-forward held). Only then may it become a `QUALIFYING_FEATURE` — and **even then it is a decomposed, inspectable axis, never a fused score.** Illustrative surfaced state (each piece independently inspectable):

```
PRICE STRUCTURE   Bearish
DEALER PRESSURE   Bearish / validated
INSTITUTIONAL FLOW  Neutral / advisory
VOLATILITY        Elevated
MARKET REGIME     Risk-off
CURRENT DECISION  NO_TRADE
```

**Rejection is a successful outcome.** If dealer pressure fails the null or the walk-forward, Scalpr has learned not to rely on an intuitively compelling but statistically useless signal — which is exactly what the infrastructure is for.

---

## Explicitly deferred (North Star, not current development)

- **Multi-ticker** (QQQ, NVDA, TSLA, …): only as separately frozen instrument/family expansions, each with its own ≥200 budget (§J).
- **Cross-sectional ranking / leaderboard:** none until multiple instruments *independently* earn qualification.
- **ES/NQ/RTY via Databento/CME:** no build until the equity-options study shows the information is worth the infrastructure and cost. Options-on-futures dealer hedging is a distinct problem, not a copy.
- **Automatic contract selection; recommendation confidence scores; autonomous execution:** deferred (confidence scores possibly never — they reintroduce the calibrated-probability we forbid).

---

## Isolation & security (restate)

- New `dealer_pressure_*` modules only; import-graph test proves no broker/order/Guard/gate/decision imports; flag default-off ⇒ zero collection.
- Raw + estimate + episode + prereg + outcome stores are new, immutable, append-only JSONL; no existing store is touched.
- No frozen cohort/config/hash altered; `UW_API_KEY` only, never persisted to source/logs/records.
- Nothing here runs on, or blocks, Monday's reversal dry run.

## Acceptance criteria

- Step 0 field map produced; reconstruction uses only confirmed fields; unconfirmed stay `UNAVAILABLE`.
- Raw prints immutable, point-in-time, per-field `DataState`; no interpretation at ingestion.
- Classification rule frozen + versioned; `UNKNOWN` never coerced; the six-variant error-sensitivity test runs and **gates** Phase 3.
- Pressure fields named `estimated_*` / decomposed; no `dealer_position`, no fused score anywhere.
- Features are separate axes; no `dealer_score`/`overall_trade_score` field exists.
- Pre-registration record is immutable, hash-stamped, and provably predates the first outcome row; episode identity enforces non-overlap; secondary horizons are non-inferential.
- Decision Replay stores realized outcomes only; predicted/expected/confidence stay null; `is_calibrated_probability: false`.
- Matched-null controls session-time + regime; walk-forward on untouched window; promotion only on the pre-registered standard.
- Isolation test passes (clean import graph); flag-off ⇒ inert; frozen state untouched; Monday unaffected.

## Open questions for Codex

1. **UW reality (Step 0):** which of the Phase-1 primitives does UW actually deliver for SPY per-trade, at what freshness — especially per-trade `delta`/`gamma` at execution, NBBO `bid`/`ask`, and `underlying_price` aligned to the trade timestamp? What's genuinely missing?
