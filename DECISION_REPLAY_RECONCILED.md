# Decision Replay — Reconciled to Scalpr (implementation contract) — Rev 3

**Status: sign-off version / implementation contract. Observation/analysis only — read-only, flag-gated, no trade authority.** Rev 3 fixes the manifest-timing blocker and three schema tightenings from review.

## Disposition legend
- ✅ **EXISTS** — reuse; do not rebuild. ➕ **EXTEND** — additive; build per the order below. 🅿️ **RESERVE** — schema slot only, `UNAVAILABLE`/null until it exists/validates. ⛔ **CONFLICTS** — record separated/raw instead.

## Two override rules (non-negotiable)
1. **No fused/weighted/combined score and no calibrated confidence.** Separated axes + explicit gates only. `combined_score`, `final_score`, `requested/calibrated_confidence`, `predicted_*`, `expected_*` → RESERVE null, `is_calibrated_probability: false`. Keep **realized** MFE/MAE/returns.
2. **No look-ahead.** Decision record uses only inputs `ts ≤ decided_at`; outcomes are forward-measured, never feed the decision; conclusions are validation-gated (cost model + no-look-ahead + session-block null).

## Rev 3 changelog
- **Blocker fixed:** the minimal **evidence-manifest writer + hash verification is built BEFORE cohort activation** (it must write at decision time, or the first cohort's evidence is unreconstructable). Only the **replay UI + explain-v0 rendering of archived manifests** stays deferred.
- **Tightening 1:** manifest references are retrievable (load-and-verify), not just a bare hash.
- **Tightening 2:** rejection **admission runs before costly contract polling**, sharing overlap controls with trade episodes.
- **Tightening 3:** gate-result records carry full identity + an immutable reason code.
- **Correction:** Data Integrity is **PARTIAL** (journal/Guard events are not universally content-hashed correction streams) — §22 no longer claims it globally.

---

## Concept A — `NoTradeTrackingPlan` + shared episode admission (order matters)

For direction/quality rejects, do the cheap admission checks **before** any contract polling:

1. **Construct** the rejection episode candidate (symbol, side, session date, reference-extreme bucket, reference level).
2. **Check the shared admission ledger** + non-overlap rule. **Trade and rejection episodes share the same overlap controls**, so one market reversal cannot repeatedly populate *both* datasets.
3. **Only if admitted**, query/select the hypothetical contract (same selection logic as a real entry).
4. **Freeze** a `NoTradeTrackingPlan` (`contract + entry_ask + target_bid + stop_bid`, at decision time) — or record **`UNTRACKABLE`** with reason if selection yields no eligible contract. Never reconstruct the counterfactual later.

Outcomes for tracked rejects use the same executable-bid engine + cost model + no look-ahead; hypothetical labels (`GOOD_REJECTION/MISSED_WINNER/AVOIDED_LOSS/NEUTRAL`) RESERVE until validated; hypothetical P&L in a **physically separate** store.

## Concept B — immutable evidence manifest (writer is PRE-activation)

**Split the work:**
- **Before cohort activation:** build the **minimal immutable manifest writer + hash verification.** At decision time it binds `decision_id` to the exact evidence used.
- **After cohort evidence exists:** build the replay UI and adapt **explain-v0 to render archived manifests** (no current-market reads).

Each manifest **reference must be retrievable**, not a bare hash:
```
manifest_reference = {
  source_type,              # feature_snapshot | micro_read | premarket | flow | ivol | contract_source | config
  schema_version,
  record_id_or_location,    # how to locate the exact frozen record
  content_hash,             # canonical_hash of that record
  observed_at, received_at, # UTC
  data_state,               # DataState
  retention_guarantee       # how long the record is guaranteed retrievable
}
```
Replay must **load the referenced record and verify its hash** before rendering. Retrieval failure or hash mismatch → that decision is **unrecoverable** (recorded as such); never fall back to current data.

---

## Section reconciliation (Rev 2 dispositions retained; changed lines noted)

1. Decision Event Model — ✅ `DecisionPacket` + append-only jsonl; ➕ thin `decision_events_v1.jsonl` referencing records.
2. Decision Run Header — ✅ most fields; enum is `CALL/PUT/NO_TRADE`; **no** `decision_status`/top-level reason (➕ add or derive); ⛔ confidence/final_score RESERVE.
3. Freeze Market State — ⚠️ PARTIAL; ➕ missing EMAs/levels; regime advisory; referenced via manifest (B).
4. Options State — ⚠️ `ContractRef` is a subset (quote/mid/spread/delta/vol/OI/strike/expiry only); IV rank / gamma-theta-vega / sweeps / flow / OI-change need frozen source refs or a versioned extension.
5. External/Event Intelligence — 🅿️ RESERVE.
6. Agent/Model Outputs — ✅ the three separated axes; 🅿️ RESERVE other agents; ⛔ don't synthesize.
7. Combined Decision — ⛔ CONFLICTS; RESERVE combined/calibrated/expected-*.
8. **Elimination Gates** — ➕ typed `gate_result` with **full identity** (Tightening 3):
   ```
   gate_result = {
     schema_version, gate_result_id, decision_id,
     gate_name, gate_version,
     result ∈ [PASS, FAIL, UNAVAILABLE, NOT_EVALUATED, NOT_APPLICABLE],
     comparator, observed_value, units, threshold,
     threshold_or_config_hash, rules_hash, config_hash,
     blocking: bool, reason_code,            # immutable enum = authoritative reason
     reason_text,                            # optional, non-authoritative
     evaluated_at, evaluation_order, source_ref
   }
   ```
   The 5-state result keeps "execution `NOT_EVALUATED` because direction failed" distinct from "execution `UNAVAILABLE`."
9. Risk Engine Events — ⚠️ PARTIAL; ✅ scope/contract/mode/paper + ratchet Guard; 🅿️ RESERVE portfolio-level fields.
10. Execution Telemetry — ⚠️ partial today (health events + journal result); missing order ID / ack-fill timestamps / latency / transitions / partials → ➕ if wanted. Shadow entry is labeled `SIMULATED`.
11. Position/Exit Telemetry — ✅ journal + `guard_events` + ratchet; shadow via `entry_bid_capture` outcome.
12. Outcome Calculation — ✅ realized MFE/MAE/returns/holding; ⛔ reserve only `predicted_*`/`expected_*` + `edge_retention`.
13. NO_TRADE Outcome Tracking — ➕ crown jewel via Concept A.
14. Replay timeline UI — ➕ deferred.
15. "Why did Scalpr trade?" — ✅ reuse **explain-v0 renderer over the manifest** (B), not the live endpoint.
16. "Why did Scalpr pass?" — ✅ same, over manifest'd gate-fail facts.
17. Raw Data Mode — ➕ cheap; label + tag record types.
18. Search — ➕ deferred.
19. Analytics — ➕ deferred; conclusions validation-gated.
20. Confidence Calibration Support — 🅿️ RESERVE (record axes+outcomes; no calibrated field).
21. Architecture/Storage — ✅ append-only; map to existing files; `no_trade_outcomes`→new; no parallel entities.
22. **Data Integrity — ⚠️ PARTIAL (correction).** Entry packets + `label_lifecycle` are hash/version-disciplined; **journal and Guard events are not universally content-hashed correction streams.** Do not claim this globally; if replay needs those hash-disciplined, that's additive work.
23. Performance — ⚠️ `guard_events` is exception-isolated but **synchronous fsync**, not non-blocking. Replay persistence uses a **bounded background queue**; never copy the sync-fsync pattern into Guard/order paths; health event on failure.
24. Safety — ✅ read-only to the Guard, flag-gated, modifies no scores/sizing/execution/kill-switches.
25. Phases — see activation order below.
26. Testing — ✅ adapted list, plus: plan freezes a contract at decision time (and `UNTRACKABLE` when it can't); admission precedes contract polling; shared trade/reject overlap prevents double-population; gate 5-state distinguishes `NOT_EVALUATED`/`UNAVAILABLE`; manifest load-and-verify reconstructs with no current read, and hash-mismatch marks unrecoverable; bounded-queue persistence never blocks execution; realized-vs-predicted separation; hypothetical P&L never touches real P&L.
27. Example acceptance — SPY; reconstruct purely from the frozen manifest; shadow fill labeled `SIMULATED`.
28. First step — ✅ this document.

---

## Corrected activation order (sign-off)

1. **Real cost model** (implement + test).
2. **`NoTradeTrackingPlan` + shared rejection/trade episode admission.**
3. **Typed gate-result records.**
4. **Minimal evidence-manifest capture + hash verification** *(pre-lock — must write at decision time).*
5. **Separate hypothetical bid/outcome store.**
6. **Tests, implementation hashes, and the pre-open cohort lock.**
7. **Start prospective capture.**
8. **Later:** timeline UI and explain-v0 over archived manifests.

## Guardrails (non-negotiable)
- No fused/combined/weighted score; no calibrated confidence; combined/calibrated/`predicted_*`/`expected_*` stay `UNAVAILABLE`, `is_calibrated_probability: false`. Keep realized MFE/MAE/returns.
- No look-ahead; outcomes never feed the decision; conclusions validation-gated.
- Append-only + correction-events (never mutate); `canonical_hash` stamps; versioned.
- Read-only to Guard/execution/scores/sizing; flag-gated; **bounded background queue** for replay persistence; health event on failure.
- Hypothetical (NO_TRADE) P&L physically segregated from real.
- Shadow fills labeled `SIMULATED`; reuse existing IDs/tables and explain-v0; **no second trading architecture.**
