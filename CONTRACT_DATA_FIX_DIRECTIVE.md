# Contract-Data Fix Directive — delta/volume assembly (for Codex)

**Problem (confirmed, isolated):** the Entry Intelligence pipeline works end-to-end (544 packets, clean manifests, 8 episodes admitted), but contract selection fails every time, so all 8 episodes are `UNTRACKABLE` and zero executable bids accrue. Root cause is a **contract-data assembly bug**, not a design flaw or a threshold problem:
- the **cached chain** has valid volume/OI/quotes but **no delta**;
- the **Alpaca snapshot path** carries greeks but **reports volume as 0** (the quote/snapshot endpoint does not carry session volume — a MISSING artifact, not a real zero);
- so every contract fails **delta AND volume** simultaneously, plus some quote-staleness/spread failures.

**Fix in one line:** assemble **one decision-time contract record by per-field source authority, joined by OCC option symbol**, frozen with provenance — never take a field from a source that doesn't actually carry it.

---

## 1. Per-field source authority (join at decision time, key = OCC symbol)

| Field | Authoritative source | Fallback | Never from |
|---|---|---|---|
| `bid`, `ask`, `spread`, quote ts | OPRA latest-quote (fresh ≤5s, two-sided) | — | stale cached-chain quote |
| `volume`, `open_interest` | option chain / daily bars | — | **quote snapshot (its 0 = MISSING artifact)** |
| `delta` (greeks) | Alpaca greeks **if fresh** | **local Black-Scholes**, labeled | cached chain (has none) |
| `strike`, `expiry`, `type`, `dte` | OCC symbol / contract metadata | — | — |

Each field in the assembled record carries its own `source`, `observed_at`/`received_at`, and `DataState` (per the field-level provenance discipline already in the flow amendments).

## 2. The rules that make this correct (not just "make it pass")

1. **Snapshot volume `0` ⇒ `MISSING`, never a real zero.** Source volume from the chain/bars. If the chain also lacks it → the contract is `UNAVAILABLE` for volume (excluded, reason recorded) — **never assume 0, never impute.** This is the crux of today's bug.
2. **Missing ≠ neutral at the field level.** If `delta` cannot be obtained (no vendor greeks and BS not computable) → contract `UNAVAILABLE`, excluded with reason. No fabricated value.
3. **Local Black-Scholes delta is allowed but labeled.** Compute from fresh underlying (IEX) + strike + time-to-expiry + IV (+ rate); stamp `delta_source: "local_bs"` vs `"alpaca_greeks"`. Prefer vendor greeks when fresh. (You already have local BS greeks.)
4. **Point-in-time / no look-ahead.** Every joined field uses only data with `ts ≤ decided_at`; timestamps frozen into the contract record.
5. **DO NOT loosen the thresholds.** delta 0.40–0.50, vol ≥200, OI ≥500, spread ≤6%, freshness ≤5s stay exactly as approved. A correctly-assembled SPY ATM 0–2 DTE contract clears vol/OI trivially. If genuinely-eligible SPY ATM contracts *still* fail after the join, that's a real finding to investigate — **not** a reason to lower gates.
6. **Quote staleness/spread failures are a separate axis.** After the join, re-check whether remaining staleness failures are real (fast market) or artifactual (429 rate-limits / inefficient fetch). If rate-limits, fix by **batching the quote fetch**, not by loosening freshness.

## 3. Config + versioning
- The field-source map and BS parameters live in the single `scalpr_config` authority, versioned.
- This changes contract-assembly logic → **bump the contract-selection version + hash** and stamp it on the assembled record. (Nothing is locked, so this is free — but stamp it.)

## 4. Acceptance tests (offline-first — you can prove the fix without a live session)

- **Replay today's 8 `UNTRACKABLE` episodes** against the fixed assembly using today's captured raw evidence/manifests: each should now **select a contract and freeze a `NoTradeTrackingPlan`** (or fail for a *legitimate* recorded reason, not "no delta"/"volume 0"). This is the primary proof.
- Unit: snapshot `volume: 0` is treated as `MISSING` and sourced from the chain; never poisons selection.
- Unit: delta obtained (vendor or labeled local BS) for a SPY ATM 0–2 DTE contract; value in 0.40–0.50 selectable.
- Unit: a genuinely eligible SPY ATM contract **PASSES** all gates end-to-end.
- Unit: a genuinely ineligible contract (real low volume / wide spread / stale quote) still **FAILS** with the correct reason and `DataState`.
- Unit: field-level provenance + timestamps recorded; every joined field `ts ≤ decided_at` (no look-ahead).
- Regression: existing 33 suites still pass; no broker/Guard/order contact added; collector stays default-off/flag-gated.

## 5. Deployment constraint
- **Build + test now**, entirely offline (the replay of today's episodes is your green light).
- **Do NOT restart while the QQQ guarded position is open** — the deploy waits for the account to be flat (`holdings == 0`; `restart_cron` already enforces this), then a controlled pre-open restart per the deployment sequence.
- The next live dry-run session is the first one that can produce real bid coverage — so the "≥ clean bid-capture sessions" clock effectively starts after this lands.

## 6. Out of scope / don'ts
- No threshold loosening; no fabricated fields; no imputed volume/delta.
- No change to the frozen cost model, the direction axis, or any locked/frozen state.
- No live trading, no Guard/execution authority; observation-only, flag-gated.
- Do not treat any post-fix captured numbers as signal — still a dry run, still cohort-ineligible until locked.
