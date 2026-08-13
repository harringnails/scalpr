# Scalpr v2 — Flow Integration Amendments (Rev 5, self-contained)

**Revision 5** supersedes Rev 1–4 and is **complete on its own** — no "see Rev 2" references. Read alongside the original change request; where this conflicts with the base spec, this wins. Rev 5 adds three governance rules (§I–§K) adopted from an external design review; nothing in Rev 4 is weakened.

**Codex-approved invariants:** evidence is not probability; missing is not neutral; UW is the sole current flow provider; everything is paper/shadow-only; raw evidence is immutable; flow cannot affect Guard exits **or trade qualification**; research uses frozen, time-aware validation.

**Rev 4 changelog (resolving the four blockers + four corrections from review):**
1. **Flow is advisory-only and lives OUTSIDE the qualifying axes** — it cannot reach a gate, even indirectly, until a future validated cohort earns it. (§A, §1c)
2. **Raw body stored as base64 of the original response bytes** with SHA-256 over those bytes — a decoded `str` cannot be byte-verbatim. (§4a)
3. **One canonical `DataState` vocabulary**, synchronized with the code (which must migrate `AVAILABLE→FRESH` etc. before activation). (§0)
4. **Self-contained**: full binding text for provider, dedupe, field-map, storage, gating, security inlined (§B–§H).
5. Decision schema carries full reproducibility set: `cohort_hash, rules_version, rules_hash, label_version, episode_version, episode_key` (config hash alone cannot reproduce a decision). (§1a)
6. Central-config requirement **scoped to new v1 systems**; frozen legacy constants are never relocated or overridden. (§5)
7. Immutability = **no silent mutation**; recorded deletion of complete **sealed partitions** is permitted for vendor retention. (§4a)
8. A fresh provider response with **zero events is an observed zero** (`FRESH`, count 0) — never `MISSING`/`UNAVAILABLE`/neutral substitution. (§0)

**Rev 5 changelog (three governance rules from external review):**
9. **Non-qualifying-signal firewall generalized** — the advisory-only rule that governs institutional flow now governs **every** non-pre-registered input, including macro/market-environment data and any additional instrument. Logged, non-qualifying, read by no axis and no gate until a new validated cohort earns it. (§I)
10. **Sample budget is per frozen hypothesis family** — the ≥200 non-overlapping-episode gate applies to each frozen hypothesis family, not in aggregate; the candidate condition set is pre-registered and frozen before looking; unlisted conditions are future out-of-sample cohorts; multiple-comparison count is recorded. (§J)
11. **Hardened data-state — never collapse to a generic pass** — `MISSING/STALE/UNAVAILABLE/UNUSABLE`/observed-zero stay distinct end-to-end; no aggregate "healthy"/"pass" may hide a late or degenerate stream; no default-to-0/neutral/false. (§K)

---

## 0. Shared vocabularies (defined once, used by every schema)

**`DataState`** — canonical, single source of truth for the state of any datum or evidence axis. Synchronized with `entry_intelligence_v1.py`, which currently uses `AVAILABLE/STALE/UNAVAILABLE` and **must migrate to this set (and update its tests) before any record is written or the subsystem is activated**:
```
FRESH        # present, valid, within freshness window   (old code value: AVAILABLE → FRESH)
STALE        # present but older than the freshness threshold
MISSING      # absent from an otherwise-successful provider response
UNAVAILABLE  # the provider/source itself was unreachable
UNUSABLE     # present but invalid (crossed / locked / nonsensical)
```
Migration mapping for the axis contract: `AVAILABLE→FRESH`; keep `STALE`; the former single `UNAVAILABLE` bucket is refined to the most specific of `MISSING / UNAVAILABLE / UNUSABLE` when determinable (per-input granularity already lives in the axis `passed/failed/unavailable` lists).

**Observed zero (Correction 8):** a successful, fresh provider response that legitimately contains **zero events/contracts** is `FRESH` with an explicit count of `0`. It is an *observation*, not absence. It must never be recorded as `MISSING`, `UNAVAILABLE`, or silently replaced with a neutral value.

**Tri-state boolean:** `true | false | null`; `null` = UNKNOWN (provider did not supply). Never default to `false`.

**Base record stamp** — every persisted record carries:
```
schema_version: str ; config_version: str ; config_hash: str
```

**Decision record stamp** — decision packets additionally carry the full reproducibility set (Correction 5):
```
cohort_hash: str ; rules_version: str ; rules_hash: str
label_version: str ; episode_version: str ; episode_key: str
```

---

## 1. v1 schemas

### 1a. `entry-intelligence-decision-v1` — per-minute decision packet
```
schema_version: Literal["entry-intelligence-decision-v1"]
config_version, config_hash                          # base stamp
cohort_hash, rules_version, rules_hash,              # decision reproducibility stamp (§0)
  label_version, episode_version, episode_key
formal_cohort_eligible: Literal[False]
decision_id, cohort_id
observed_at, decided_at: datetime                    # UTC; all evidence ts <= decided_at
symbol, setup_type
decision: Literal["CALL","PUT","NO_TRADE"]           # default NO_TRADE
scores: EvidenceScoreBlock                           # §1b — three SEPARATE, mechanical/execution axes
selected_contract: ContractRef | None
frozen_target: LevelRef | None                       # REQUIRED iff CALL/PUT; None (with reason) for NO_TRADE
frozen_invalidation: LevelRef | None                 # REQUIRED iff CALL/PUT; None for NO_TRADE
supporting_evidence, opposing_evidence: list
missing_or_stale: list                               # NO_TRADE must carry a missing/stale or failed reason
raw_evidence_refs: list[str]                         # provenance into §4a
advisory_flow_context: AdvisoryFlow | None = None    # §1c — NON-qualifying; no gate reads this
```
Validators (as implemented): `decided_at >= observed_at`; `NO_TRADE` ⇒ target/invalidation `None` and a missing/stale/failed reason present; `CALL/PUT` ⇒ contract + target + invalidation present.

### 1b. `EvidenceScoreBlock` — three separated axes, **no fused score, no flow**
```
class EvidenceAxis:
    status: DataState                 # FRESH/STALE/MISSING/UNAVAILABLE/UNUSABLE
    value: float | None = None        # MUST be None unless status == FRESH
    score_version: str
    passed: list[str] ; failed: list[str] ; unavailable: list[str]
    is_calibrated_probability: Literal[False] = False

direction: EvidenceAxis      # MECHANICAL technical reversal only — no institutional flow input
quality: EvidenceAxis
executability: EvidenceAxis
is_calibrated_probability: Literal[False] = False
# No aggregate / overall / confidence field exists. Ranking = transparent tier rules over the axes.
```

### 1c. Institutional flow — advisory-only (Blocker 1)
`institutional-flow-event-v1` and `institutional-flow-snapshot-v1` feed **only** an `AdvisoryFlow` context that is recorded for observation and **read by no gate and no axis**:
```
class AdvisoryFlow:
    schema_version, config_version, config_hash
    advisory_flow_direction: Literal["BULLISH","BEARISH","NEUTRAL","UNKNOWN"] = "UNKNOWN"
    availability: DataState            # incl. STALE; observed-zero = FRESH,count 0
    is_calibrated_probability: Literal[False] = False
    is_qualifying: Literal[False] = False   # explicit: never enters direction axis / gate
```
`institutional-flow-event-v1` (normalized event): base stamp; `provider: FlowProvider` (enum, §C); `event_id: str | None`; `raw_id` (FK → §4a); `observed_at`/`received_at` UTC; `dedupe_key` (§B); market fields; `execution_side ∈ {ASK,BID,MID,ABOVE_ASK,BELOW_BID,UNKNOWN}=UNKNOWN`; `sentiment ∈ {BULLISH,BEARISH,NEUTRAL,UNKNOWN}=UNKNOWN`; `is_sweep/is_block/is_multi_leg: bool | None = None` (tri-state). Any field UW does not supply stays `null`/`UNKNOWN` (§E).
`institutional-flow-snapshot-v1`: base stamp; `availability: DataState` (STALE preserved, observed-zero honored); `data_quality: DataState`; rolling values `float | None`; `is_calibrated_probability: Literal[False]`.

Flow may only become qualifying via a **new, validated cohort** that explicitly earns it under §D — not by default in v1.

### 1d. Ledgers — §4.

---

## §A. No fused score; flow advisory-only

- **No weighted "overall confidence" is produced or persisted.** The 20/25/25/15/10/5 weights are removed. A single scalar over evidence categories is a pseudo-probability and is forbidden.
- The three axes (direction / quality / executability) stay **separate and independently surfaced**, each able to be non-`FRESH` with `value: None`.
- Act/no-act is a **transparent, explicit tier** over the separate axes (e.g., all three `FRESH` and above per-axis thresholds), reproducible and decomposable — never a blend.
- **Institutional flow is advisory-only (Blocker 1).** It does not feed the qualifying `direction` axis, nor any other axis, nor any gate. Because a gate reads the three axes, letting flow move an axis would qualify indirectly — so flow is kept in `AdvisoryFlow` (§1c) with `is_qualifying=false` until a future validated cohort earns inclusion.

## §B. Deduplication — provider-namespaced IDs + stable fallback
- **Primary key:** provider-namespaced event id — `f"{provider.value}:{event_id}"` when `event_id` is present. (Prevents cross-provider id collisions.)
- **Fallback (no event_id):** a stable 256-bit `fe.canonical_hash` over normalized fields — `observed_at` in UTC at fixed precision, `Decimal` quantized to a fixed exponent and serialized as string, ints as ints, `provider`, `option_symbol`, `trade_price`, `trade_size`, `premium`, `venue`. Never Python `hash()` (randomized for str/bytes; 64-bit truncation → collisions).
- Duplicates are written to the rejection ledger (§4b, reason `DUPLICATE`), never silently dropped. Dedupe-store TTL is configurable.

## §C. Provider model is an extensible enum, not `Literal`
```
class FlowProvider(str, Enum):
    UNUSUAL_WHALES = "unusual_whales"     # only implementation today
    # future providers add here — no schema redesign
provider: FlowProvider
```
`UnusualWhalesAdapter` is the sole adapter now; the model stays provider-neutral.

## §D. Feature pre-registration + time-aware null (reuse principles, not the function)
- Pre-register a **small primary feature set** before generating the full rolling surface; everything else is exploratory and labeled so.
- The regime-bar null in `regime_research.py` is **not reused directly**. Entry Intelligence uses a cohort-specific, session-aware test over **non-overlapping candidate episodes** (`entry-episode-session-block-sign-null-v1`): contiguous session blocks share a random sign to preserve within-session dependence; one-sided p-value with the finite-sample `+1` correction.
- Any feature definition used in a comparison is **frozen and versioned**. No blending of research versions. Flow earns qualification only by clearing this null in a dedicated cohort (§A).

## §E. Missing ≠ neutral at the FIELD level + available-field map
- Fields UW may not expose (`execution_side ∈ {ABOVE_ASK,BELOW_BID}`, per-event `is_block`, clean `is_multi_leg`) stay `UNKNOWN`/`null` — never inferred, never defaulted to a directional value or `false`.
- Phase 1 audit must produce an explicit **available-vs-unavailable field map** before the normalizer promises any field. Known-available surface today: `ask_pct, net_aggressor, sweep_vol, oi_chg, oi_since_prev, multileg_pct, gex-levels, darkpool, iv_rank, max_pain`. Treat anything beyond as unverified until confirmed.
- Every rejected/malformed/stale/duplicate event is recorded in the rejection ledger (§4b) — never silently dropped.

## §F. Storage — keep append-only discipline; justify a DB before adopting one
- Raw capture stays **append-only, atomic (`_atomic_append`), tolerant-reader**, regardless of backing store. A DB migration must not weaken existing crash-safety.
- Any relational store is **additive**; it does not replace or alter the JSONL stores the frozen cohorts read from.

## §G. Additive, flag-gated, never touches frozen state or the Guard
- All ingestion, features, and advisory scoring are **behind a feature flag** (`institutional_flow.enabled`) and **read-only w.r.t. the live ratchet Guard**. Flow never enters a live exit path **or trade qualification**.
- **Do not alter** frozen items: incubation hash `3425809…`, incubation primary cells, 10/10/15 materiality on executable bid, `scalpr-intel-v0`, 900/3600 quote-staleness, wave/flow versions, and the ratchet-only Guard (no separate hard stop). Any change forces a new cohort.
- Everything is **paper/shadow only**; live enablement is a separate, gated decision.

## §H. Security — retain `UW_API_KEY`
- Secret env var is **`UW_API_KEY`** (retain this exact name), loaded from environment / approved secrets manager only. Never written to source, logs, test fixtures, raw/rejection ledgers, DB records, or error payloads. Provider errors logged with headers/credentials stripped.

---

## §I. Non-qualifying-signal firewall — generalizes advisory-only to ALL non-registered inputs (Rev 5, Rule 9)

The advisory-only discipline that governs institutional flow (§A, §1c, §G) is the **general rule for every input that is not a pre-registered qualifying feature of the frozen cohort**, not a special case for UW. This explicitly includes:

- **Macro / market-environment data:** VIX and the volatility complex, index-divergence, breadth, VIX-futures term-structure shape, quarterly/seasonal context.
- **Any additional instrument beyond the cohort's own symbol:** QQQ, DIA, IWM, VXX, VIXY, UVXY, SVOL, etc.

**Binding requirements:**
1. Such inputs are captured **advisory-only**: recorded, timestamped with provenance, and carried in a non-qualifying context (`is_qualifying: false`, `is_calibrated_probability: false`), exactly like `AdvisoryFlow` (§1c). They are **read by no axis and no gate** — not `direction`, not `quality`, not `executability`, not any act/no-act tier.
2. There is **no path** by which adding a feed silently influences a decision. The default for any new input is advisory; qualification is opt-in and gated, never automatic.
3. An advisory input becomes qualifying **only** by clearing the §D/§J validation inside a **new, dedicated, frozen cohort** that explicitly names it. Adding it to an existing cohort is forbidden (forces a new cohort id, §G).
4. **No fusion.** Advisory inputs are never blended into a score or a confidence. Correlated instruments in particular (equity-beta ETFs ≈ one factor; the VIX/VXX/VIXY/UVXY/SVOL complex ≈ one factor) may enter a future test **only as an explicitly-defined derived feature** (e.g., index-divergence, vol-regime, term-structure state), one frozen hypothesis at a time — never as a raw multi-feed enrichment of the decision axes.

## §J. Sample budget + multiple-comparisons control (Rev 5, Rule 10)

The ≥200 non-overlapping labeled-episode gate (§D) is a floor **per frozen hypothesis family, not in aggregate**.

1. **Per-family budget.** Each conditioned variant — e.g. `reversal × vol-regime`, `reversal × index-divergence` — is its own hypothesis family and requires its **own** ≥200 non-overlapping episodes. 200 total does not license a menu of conditioned claims.
2. **Pre-register then freeze.** The candidate condition set is written down and frozen **before** examining outcomes. Any condition not in the frozen set is treated as a **future out-of-sample cohort**, never mined from data already collected. "We noticed X in the data, so we added X" is prohibited without a fresh prospective test.
3. **Explicit multiple-comparison accounting.** When one dataset is used to screen several pre-registered conditions, the **number of conditions tested is recorded** and no edge is claimed on a minimum p-value without a stated correction. The controlling session-block null (§D) applies to each family.
4. **No silent family expansion.** Adding a condition, instrument, or macro feature to the qualifying set is a new cohort id (§G) with its own prospective clock.

## §K. Hardened data-state — never collapse to a generic pass (Rev 5, Rule 11)

Extends §0 and §E from the field level to **every aggregation and gate**.

1. `FRESH` (incl. observed-zero, count 0), `STALE`, `MISSING`, `UNAVAILABLE`, and `UNUSABLE` are **distinct end-to-end** and must never be reduced to a single generic `pass`/`ok`/`healthy`/`true`.
2. A session, contract, or axis may be aggregate-"healthy" while an **individual stream is late or degenerate**. Any gate that reduces multiple per-field states to one verdict **must retain the per-field state and reason** in the record; the verdict may not erase which inputs were `MISSING`/`STALE`/`UNUSABLE`.
3. **No default substitution.** No default-to-0, no default-to-neutral, no default-to-`false`. A `MISSING` artifact is never read as a real value. (This is precisely the class of the snapshot-`volume:0` bug: a missing datum reported as `0` must be treated as `MISSING`, never a real zero.)
4. Applies to the direction axis inputs (bars), the executability axis (OPRA quotes), advisory inputs (§I), and any feed-quality or cohort-lock gate that summarizes them.

---

## 4. Raw + rejection ledgers (immutable; append-only; base stamp on each)

### 4a. `raw-evidence-record-v1` — raw ingestion ledger (byte-verbatim, Blocker 2)
```
schema_version, config_version, config_hash
raw_id: str
provider: FlowProvider ; endpoint: str ; request_id: str | None
received_at: datetime ; http_status: int | None
content_type: str | None
content_encoding: str | None                # e.g. gzip/br as received
raw_body_base64: str                        # base64 of the ORIGINAL response.content BYTES — the source of truth
raw_body_sha256: str                        # SHA-256 over the original bytes (NOT over text or a dict)
parsed_view: JSONValue | None               # LOSSY convenience parse; non-authoritative; may be null; never hashed/reprocessed
ingest_batch_id: str
```
(Alternative permitted: store the original bytes as a separate immutable blob and reference its digest/path here.)

**Immutability & retention (Correction 7):** records are **never silently mutated**. Reprocessing/backfills read `raw_body_base64`, not `parsed_view`. To satisfy vendor retention limits, deletion is allowed **only as a recorded deletion of a complete sealed partition** (whole time-bounded segment), logged with reason and range — never a silent per-record edit or partial rewrite.

### 4b. `rejection-record-v1` — rejection ledger (nothing silently dropped)
```
schema_version, config_version, config_hash
rejection_id: str ; received_at: datetime
provider: FlowProvider ; raw_id: str | None
reason: Literal["MALFORMED","MISSING_REQUIRED_FIELD","SCHEMA_MISMATCH",
                "DUPLICATE","STALE","OUT_OF_DECISION_WINDOW"]
offending_fields: list[str] ; detail: str ; raw_excerpt_sha256: str | None
```
Observability counters derive from this ledger, not ad-hoc logs.

---

## 5. One authoritative configuration source — scoped to new v1 (Correction 6)

- The **new v1 subsystem** reads all thresholds, windows, TTLs, and flags from a single authority (`scalpr_config` → one `config/scalpr.yaml`), read once, validated, stamped `config_version` + `config_hash = fe.canonical_hash(resolved_config)`, exposed read-only. Within the v1 subsystem, duplicated constants are a defect.
- **Frozen legacy constants are NOT relocated or overridden.** The existing frozen module constants backing locked cohorts (`scalpr-intel-v0`, incubation `3425809…`, wave, feature_engine staleness, etc.) stay exactly where they are; the authority may *reference* them but must never move, restate, or supersede them.
- `low_reversal_v1` `820c1694…` is a **DRAFT change-detection hash, NOT an operational lock**, and is excluded from the locked set until its blocking findings are resolved and it is deliberately locked pre-session via the fail-closed lock registry.

---

## Acceptance criteria (Rev 4)

- Institutional flow is advisory-only, `is_qualifying=false`, and reaches no axis or gate; qualifying `direction` axis is mechanical technical only.
- Raw body is base64 of original response bytes with SHA-256 over those bytes; `parsed_view` is lossy/non-authoritative; immutability allows only recorded sealed-partition deletion, never silent mutation.
- One canonical `DataState` (incl. `STALE`, `MISSING`, `UNUSABLE`) used everywhere; code + tests migrated from `AVAILABLE/STALE/UNAVAILABLE` before activation; observed-zero = `FRESH`,count 0.
- Document self-contained: provider enum, provider-namespaced dedupe, field map, storage, gating, security all inlined.
- Decision packet stamps `config_*` **and** `cohort_hash/rules_version/rules_hash/label_version/episode_version/episode_key`.
- No fused/weighted confidence anywhere; three axes separate; nullable score values; tri-state provider booleans; `NO_TRADE` carries no target/invalidation but a reason; `CALL/PUT` carries both.
- Central config scoped to new v1; frozen legacy constants untouched; `low_reversal_v1` hash draft-only.
- Raw + rejection ledgers immutable/append-only; secret env var `UW_API_KEY` never persisted.
- Entire subsystem flag-gated, read-only to the Guard, paper/shadow only; no frozen cohort/config/hash altered.
- **(Rev 5) Non-qualifying firewall generalized:** macro/market-environment data and any extra instrument are advisory-only (`is_qualifying=false`), reach no axis or gate, and can qualify only via a new validated cohort; correlated feeds enter only as an explicit derived feature, one frozen hypothesis at a time — never a fused enrichment. (§I)
- **(Rev 5) Sample budget per family:** ≥200 non-overlapping episodes **per frozen hypothesis family**; condition set pre-registered and frozen before looking; unlisted conditions are future out-of-sample cohorts; number of tested conditions recorded; family expansion forces a new cohort id. (§J)
- **(Rev 5) Data-state never collapsed:** `FRESH`(incl. observed-zero)/`STALE`/`MISSING`/`UNAVAILABLE`/`UNUSABLE` stay distinct through every aggregation and gate; per-field state + reason retained in any verdict; no default-to-0/neutral/false; a `MISSING` datum is never read as a real value. (§K)
