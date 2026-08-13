# Scope Expansion Directive — restore the wide MANUAL paper builder

**Operator decision (2026-08-06):** restore the broader manual paper builder that the pre-V2 platform had, **without widening any automated system.** Out-of-scope manual positions remain ratchet-managed, but flagged and isolated. Paper/shadow only.

This directive supersedes the blanket SPY-only lock in `scope_policy.py` for the **manual path only**. It requires a new `SCOPE_VERSION` and a new ADR (next number, ≈ADR-019).

---

## 1. Two-tier scope (the core change)

Split scope into two explicit checks. Fail-closed remains the default for everything not explicitly manual.

**Manual paper builder (operator-initiated, one position at a time):**
- Any Alpaca-supported **optionable** equity underlying.
- Options **0–60 DTE**.
- Every contract **validated through Alpaca before submission** — if the contract isn't found/tradeable, reject (no fabricated or illiquid contracts).
- (Stock trades: the old menu also had a `Stock` type. NOT included here by default — confirm separately if you want manual equity shares restored, since the ratchet behaves differently on shares than on options.)

**Automated systems — UNCHANGED, still SPY 0-2 DTE (their existing validated scopes):**
- Automated direction, Entry Intelligence, the forward bid collector, Wave additions/observer, and the institutional-flow + IVolatility capture workers.
- These continue calling the **narrow** scope check. This directive must not touch their scope by even one field.

Implementation: keep the current narrow validator as the automated/default path; add a separate `validate_manual_trade(...)` (wide scope) used **only** by the manual builder endpoint. New `SCOPE_VERSION`, e.g. `scalpr-manual-wide-options-0-60dte-v2`; the narrow automated `scalpr-spy-options-0-2dte-v1` stays as-is and remains the default.

## 2. Guard behavior on out-of-scope manual positions (operator choice: Guard-managed, flagged)

- Out-of-scope manual positions **are** managed by the ratchet Guard (protection preserved — the whole-position ratchet may auto-sell 100% per ADR-015; the manual entry acknowledgment still applies).
- The ratchet uses the **same parameters** — no silent re-tuning. Because the ladder was tuned/observed on SPY 0-2 DTE, tag these positions so it's explicit that their ratchet behavior is **unvalidated** (it may exit slow, longer-dated, wider-spread names prematurely).
- Tag the Guard and every journal row with a scope flag, e.g. `scope_class: "manual_out_of_envelope"` (validated SPY 0-2 DTE positions keep `scope_class: "validated"`).
- **Live mode stays blocked** — paper/shadow only.

## 3. Data isolation (must not corrupt frozen cohorts)

- `scope_class: "manual_out_of_envelope"` outcomes are **excluded** from every validated metric and every cohort: Cohort A–E, Entry Intelligence candidates/labels, incubation, and any "best"/edge computation. They never count toward the 200-candidate gate or any frozen-cohort statistic.
- They are recorded as normal paper journal rows for the operator's own review, clearly separable by the flag.
- No frozen hash, cohort, or the `scalpr-intel-v0` / incubation `3425809…` / wave versions are altered.

## 4. UI

- Restore the manual builder menu: underlying entry (any optionable equity), option type, and expirations through 60 DTE.
- On any non-SPY or >2 DTE selection, show a visible label: **"Out of validated scope — ratchet is tuned for SPY 0-2 DTE; behavior unvalidated."**
- Automated panels (Entry Intelligence, Wave, flow) show no scope change.

## 5. ADR

Add a decision record (≈ADR-019): "Restore wide manual paper builder (any optionable equity, 0-60 DTE), Alpaca-validated. Out-of-scope manual positions are ratchet-managed but flagged `manual_out_of_envelope` and isolated from all cohort data. Automated direction, bid collector, Wave, Entry Intelligence, and provider capture remain SPY 0-2 DTE. Live blocked; paper/shadow only." Reference this directive and the operator confirmation.

---

## Acceptance criteria

- Manual builder accepts any Alpaca-supported optionable equity, 0–60 DTE; each contract is Alpaca-validated before submission; unfound/untradeable contracts are rejected.
- Automated direction, Entry Intelligence, bid collector, Wave, and provider-capture scopes are byte-for-byte unchanged (still SPY 0-2 DTE); the narrow validator remains the default/fail-closed path.
- Out-of-scope manual positions are ratchet-managed with unchanged parameters and tagged `scope_class: "manual_out_of_envelope"`; validated positions tagged `scope_class: "validated"`.
- Flagged positions are excluded from every cohort/validated metric and the 200-candidate gate; no frozen cohort/hash altered.
- Live trading remains blocked; everything paper/shadow only.
- New `SCOPE_VERSION` and ADR added; UI shows the out-of-scope warning label.
