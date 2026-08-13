# Scalpr — Task List to MVP

**Created:** 2026-08-12
**Mode:** paper / shadow only throughout.

## What "MVP" means here (read first)

MVP is **not** "a profitable strategy" and **not** "go live." Given the evidence to date — no signal has demonstrated edge anywhere, and realized paper P&L is negative and tail-dominated — the honest MVP is:

> A research-grade platform that can **safely and honestly determine whether any signal has a real edge**, with downside protected.

**MVP is reached when all four are true:**
1. Downside is capped in paper (hard stop working).
2. Evidence pools are clean and separate.
3. A labelable outcome basis exists.
4. At least one signal has an honest edge verdict (yes/no) against a null + walk-forward.

**Live money is a separate, later milestone** gated on a proven edge that does not exist yet.

**Guardrails (every phase):** paper/shadow only; no edge claims before the null + walk-forward clear; no threshold loosening to force results; any direction change is a new frozen cohort; pre-register before you look.

---

## Phase 0 — Finish the good state (housekeeping)

- [ ] Complete the git baseline commit on the Mac (repo initialized, `.gitignore` protects secrets); push/copy off-machine. **Done =** committed + backed up externally.
- [ ] Verify then `rm scalp.keys` (creds load from Keychain now). **Done =** no plaintext key on disk.

## Phase 1 — Downside protection: Guard asymmetric hard stop (highest ROI, no edge assumption)

Spec finalized: `GUARD_ASYMMETRIC_HARD_STOP_SPEC_2026-08-12.md`.

- [ ] **Honest replay characterization.** Pick the stop level as a *risk decision* first; run it over the 239-trade journal **once**; report **both** tail-loss reduction and winners-clipped / spurious-stop rate. No level sweeping on the same journal. **Done =** replay report showing both sides.
- [ ] **Decide** whether to wire it in and at what level (pre-committed). **Done =** go/no-go + chosen level.
- [ ] **Implement as a separate Guard path** (config-gated, paper only): loss cap with 2-fresh-tick confirmation; degradation exit at 10–15s of no fresh two-sided mark; asymmetric trail computing peak + giveback on fresh confirmed marks only; distinct journaled exit reason; not bypassed by pause/resume. **Done =** code + tests (whipsaw, degradation, confirmation).
- [ ] **Verify in paper** over a few sessions: fires on real losers, no whipsaw on quote noise, no spurious winner-clipping. **Done =** paper confirmation.

## Phase 2 — Clean the evidence pools (discipline)

- [ ] Formally scope and label the four pools — manual Guard, wave shadow, incubation, entry-intelligence — as **separate** evidence, never one edge story; no cross-pool pooling in any analysis. **Done =** each pool's scope/purpose documented and enforced.

## Phase 3 — Labelable outcome basis (A2)

Stage A closed as **A2**: the single-option executable-bid basis is not reliably viable (`STAGE_A_OUTCOME_BASIS_VIABILITY.md`).

- [ ] **Pre-register the A2 alternative outcome basis.** Recommendation: **underlying-forward-return** over greeks-mapped (0DTE gamma/theta make the greeks map inaccurate on exactly the big moves). **Done =** frozen A2 basis spec with bias controls.
- [ ] **Build/validate the A2 measurement** — reuse the existing `entry_policy_prototype` `return_5m/15m/30m/60m` + MFE/MAE machinery. **Done =** a labelable outcome pipeline independent of single-option quote density.

## Phase 4 — The MVP question: does any signal have edge?

- [ ] **Run the cheap directional-edge test.** Does the reversal setup (frozen, as-is) predict the underlying's forward move on the A2 basis, against a **session-block matched null + walk-forward**? **Done =** honest edge / no-edge verdict.
- [ ] **Branch on the result:**
  - **No edge** → a real result. Pivot effort to the denser-signal order-flow studies (dealer pressure / CVD), not more reversal variants.
  - **Edge** → proceed to reversal v2 direction (new frozen cohort, *smallest* relaxation of `momentum_slowing` / `causal_confirmation`), liquidity-first contract selection, and cost-adjusted expectancy — per `REVERSAL_V2_DESIGN_BRIEF.md`.

---

## Parallel / standing research tracks (not on the MVP critical path)

- **Dealer Pressure Advisory Study** (`DEALER_PRESSURE_ADVISORY_STUDY_SPEC.md`) — UW authenticated; next is the real Step 0 field audit (are per-trade delta/gamma/underlying point-in-time?). Denser-signal candidate.
- **IVolatility research annex** (`IVOLATILITY_RESEARCH_ANNEX_PROPOSAL.md`) — IVOL authenticated; Stage P/D fidelity probe when a reference capture exists.
- **CVD-Lab** (separate repo) — Databento MES data engineering strong; detector v2 fell short of the 100-episode floor (96); freeze v2, test on fresh out-of-sample sessions.

## Ordering rationale

Phase 1 is the only phase that improves realized outcomes **today** (and needs no edge assumption). Phases 3–4 are what determine whether there is anything real to build on at all. Everything else waits on the Phase-4 verdict.
