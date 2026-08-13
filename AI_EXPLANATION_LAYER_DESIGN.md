# Design Proposal — AI Explanation Layer (`explain-v0`) — Rev 3

**Status: implementation-ready design for Codex, pending sign-off. Paper/shadow, read-only, non-authoritative, default-off. Not an evidence axis, not a signal, not a gate.**

Purpose (Rev 2 wording retained): **help the user inspect the deterministic evidence** — not "guide the manual decision," because presentation influences decisions even when labeled non-authoritative.

**Rev 3 changelog — four contract fixes:**
1. **No model-controlled omission or emphasis.** Code selects `narratable_fact_ids` and assigns each fact's section + priority; the model may only reorder within equal-priority ties. No add/omit/re-section (Fix 1).
2. **Server-bound brief hash.** The freshness hash is bound to the request server-side, never trusted from model output; cache key is a `canonical_hash` over a labeled object (Fix 2).
3. **The renderer is versioned.** `renderer_version` + `template_registry_hash` added to the cache key and every record stamp; changing a sentence template invalidates cached plans (Fix 3).
4. **Complete mandatory coverage** incl. `UNUSABLE` and the full required set; HTML-escape free text in the raw brief (Fix 4).

*(Rev 2 basis, unchanged: model plans / code writes the sentences; enum-typed-only model input; traceable-not-deterministic; regex is a tripwire only.)*

---

## Architecture

```
Evidence Brief with immutable fact_ids   (code-generated, typed/enum only; source of truth)
        │
        ▼   code selects narratable_fact_ids and assigns each a (section, priority)
Sanitized fact table + section/priority assignments
        ▼   LLM returns a NarrationPlan = a PERMUTATION restricted to reordering
        │   within equal-priority ties inside each code-assigned section
        ▼
Deterministic renderer  →  each fact_id → its ONE code-owned sentence template
        ▼
Coverage / order / freshness / intent validator
        │ pass                              │ fail | stale | model down | refusal
        ▼                                   ▼
   display rendered prose            render the raw Evidence Brief (always available)
```

**The model's entire influence is reordering equal-priority facts and picking a formatting style.** It cannot choose which facts appear, which section they land in, their priority, or their wording. Omission-slant and emphasis-slant are therefore removed, not just fabrication. Honest note: this makes the layer ~fully deterministic — the model is an optional cosmetic reorderer, and the whole feature degrades cleanly to a pure-code renderer with zero model calls. That is the intended end state for a safety-first explainer.

## Evidence Brief with fact IDs

Deterministic pre-formatter emits atomic, code-owned facts; each `fact_id` has exactly one code-owned rendering template. Types: `state | count | number | symbol | timestamp | enum_label`.
```
{ "fact_id": "ei.direction.status", "type": "state", "value": "FRESH", "state": "FRESH" }
{ "fact_id": "flow.uw.state",       "type": "state", "value": "UNAVAILABLE", "state": "UNAVAILABLE" }
{ "fact_id": "ei.decision",         "type": "enum_label", "value": "NO_TRADE" }
```

## Code-selected narration set (Fix 1)

Before any model call, code computes:
- `narratable_fact_ids`: the exact set of facts eligible for narration (includes all mandatory-coverage facts, below).
- For each, a `section` (`SectionEnum`) and integer `priority`.

The model receives that set + assignments and returns a plan. **Validator requires the plan's per-section fact multiset to equal the code-assigned set (no additions, no omissions), and ordering to respect priority — only equal-priority items may be swapped.** Any cross-section move, priority violation, add, or omit → fallback to raw brief. `StyleEnum` affects **formatting only** (e.g., bullet vs sentence), never wording or emphasis.

## Model input — sanitized (unchanged from Rev 2)

Only code-owned enums/reason-codes, typed numbers/timestamps/symbols/`DataState`, pre-approved labels, and the `SectionEnum`/`StyleEnum` vocabularies. **Prohibited:** raw payloads, `notes`/reason free-text, news, API/error messages, account info, P&L, quantities, keys. Free text that exists in the full brief is rendered deterministically for display, **never** sent to the model.

## NarrationPlan (strict structured output)

```
NarrationPlan = {
  sections: [ { section: SectionEnum,
                items: [ { fact_id: str, style: StyleEnum } ] } ]
}
```
No `input_brief_hash` in the model output — it is **server-bound** (Fix 2). Structured-output schema adherence guarantees *shape only*; semantic safety comes from code owning the sentences and the permutation constraint.

## Freshness / stale-output barrier (Fix 2)

- The server computes `brief_hash = canonical_hash(labeled_brief_object)` and stores it in the **request envelope** (server-side), keyed to the request id. The model never supplies it; any hash in model output is ignored.
- Immediately before display, the server-bound `brief_hash` for that request must equal the **currently visible** brief hash. A late response for an older market state is **discarded, never shown beside newer evidence.**

## Deterministic renderer + fixed sections

For each `fact_id` in the (validated) plan, emit its canonical templated sentence filled from the typed value; concatenate per section:
1. **Data quality** — fresh/stale/missing; whether it forces abstention.
2. **What the mechanical layers found** — the three Entry Intelligence axes by status; Market Read / premarket / regime, descriptively.
3. **What's missing or advisory** — UW/IVol unavailable, regime descriptive-only, contract availability.
4. **The system's own output** — the decision packet restated (`NO_TRADE` + reason, or `CALL/PUT` + contract), never overridden.
5. **Reading note (fixed, non-directional)** — "Descriptive only. Not a probability, signal, or recommendation. Missing inputs are unknown, not neutral."

## Mandatory coverage (Fix 4)

`narratable_fact_ids` must include (and the plan must therefore contain) every one of:
- Every fact whose state is **non-FRESH** — `STALE`, `MISSING`, `UNAVAILABLE`, **`UNUSABLE`** (the previously omitted one).
- `regime.is_descriptive_only`, `flow.is_qualifying`, `data_quality.forces_abstain`.
- The **decision and its reason**.
- **Both** supporting **and** opposing mechanical evidence (code-selected, so neither side can be dropped).

Any omission → fallback. Separately, **HTML-escape any free text shown in the raw brief** display path, even though it never reaches the model (XSS/display defense).

## Cache key, reproducibility & retention (Fix 3 + Fix 2)

- **Cache key** = `canonical_hash({ brief_hash, prompt_hash, schema_hash, validator_hash, renderer_version, template_registry_hash, provider, model_snapshot_id })` — a labeled object, not string concatenation.
- **Record stamp** on every narration: `explain_version, prompt_hash, schema_version, validator_version, renderer_version, template_registry_hash, model_snapshot_id, input_brief_hash` (server-bound), plus raw plan + validator result. Append-only.
- **Renderer is load-bearing:** changing any sentence template changes `template_registry_hash`, which invalidates cached plans and produces a new traceable version.
- **Traceable, not deterministic** — pinned snapshot + temperature 0 improves stability, not identity.
- **Retention:** for OpenAI, `store: false` prevents Responses **application-state** storage, but **ordinary abuse-monitoring retention can still apply unless separate approved controls (e.g. ZDR) exist.** Send no PII/account/P&L regardless. Provider key via env/secrets manager only (as `UW_API_KEY`), never logged.

## Gating

Default-off flag (`EXPLAIN_LAYER_ENABLED`, unset = off). Off ⇒ deterministic brief only, and **zero outbound model calls**.

## Acceptance tests

- **Omission/emphasis slant** → plan that drops or re-sections a mandatory fact, or violates priority, is rejected.
- **Semantic inversion** with valid tokens → impossible (code writes sentences); assert renderer output == templates.
- **Cross-symbol contamination** → plan referencing another symbol's fact_id rejected.
- **Late async / stale hash** → server-bound hash mismatch discarded; model-supplied hash ignored.
- **Missing-state omission** incl. `UNUSABLE` → coverage fails → fallback.
- **Number-format variants** (`771`, `771.0`, written) → moot (code renders values); rejected if in model output.
- **Prompt injection in note fields** → notes never sent; sanitizer strips them; assert.
- **Refusal / truncation / invalid structured output** → fallback.
- **Template change** → `template_registry_hash` changes, cache invalidated, new version stamped.
- **Flag off** → zero outbound model calls (network assertion).
- **Import graph** → broker/order/Guard/scope modules absent from `explain-v0`'s full import graph.
- **HTML escaping** → free text in raw brief is escaped.

## Explicit non-goals

Not an evidence axis; not persisted as decision input; not read by any gate, cohort, or the Guard. Not a probability/recommendation engine. Not the calibrated-ML directional model (separate, later, data-gated). Not a replacement for the deterministic brief.

## Sources

- [OpenAI — Structured Outputs (schema conformance, not semantic grounding)](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI — Models / snapshots](https://developers.openai.com/api/docs/models)
- [OpenAI — Data controls (`store:false`; abuse-monitoring retention ≠ ZDR)](https://developers.openai.com/api/docs/guides/your-data)
