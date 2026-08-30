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

## Current single gate (2026-08-13)

Everything is built and tested in the research shell — A2 labeler, cross-side collision remediation (quarantine + fail-closed double-qualification guard + A2 exclusion), regime layer v0, and the Stage-1a viability report. **The entire board is blocked on one operator action: restart the collector onto v1.2 at end-of-day (flat-account gated), verify the runtime reports `v1.2`, and confirm a post-restart collision scan of 0** — see `COLLECTOR_V1.2_RESTART_RUNBOOK.md`. The source is already v1.2; the live runtime is a stale v1.1 process, so a clean restart is the whole fix. Until then, zero clean prospective episodes accrue and Stage-1a correctly reads `INSUFFICIENT_TAGGING`. After the restart, the clean accrual clock starts; the ≥200-episode gate is ~12–18 months out at the v1 fire rate (the open Reversal-v2 timeline decision, not an engineering task).

---

## Phase 0 — Finish the good state (housekeeping)

- [x] Complete the git baseline commit on the Mac (repo initialized, `.gitignore` protects secrets); push/copy off-machine. **Done =** committed + backed up externally.
- [x] Verify then `rm scalp.keys` (creds load from Keychain now). **Done =** no plaintext key on disk.

## Phase 1 — Downside protection: Guard asymmetric hard stop (highest ROI, no edge assumption)

Spec finalized: `GUARD_ASYMMETRIC_HARD_STOP_SPEC_2026-08-12.md`.

- [x] **Honest replay characterization.** Pick the stop level as a *risk decision* first; run it over the 239-trade journal **once**; report **both** tail-loss reduction and winners-clipped / spurious-stop rate. No level sweeping on the same journal. **Done =** replay report showing both sides.
- [x] **Decide** whether to wire it in and at what level (pre-committed). **Done =** go/no-go + chosen level.
- [x] **Implement as a separate Guard path** (config-gated, paper only): loss cap with 2-fresh-tick confirmation; degradation exit at 10–15s of no fresh two-sided mark; asymmetric trail computing peak + giveback on fresh confirmed marks only; distinct journaled exit reason; not bypassed by pause/resume. **Done =** code + tests (whipsaw, degradation, confirmation).
- [ ] **Verify in paper** over a few sessions: fires on real losers, no whipsaw on quote noise, no spurious winner-clipping. **Done =** paper confirmation.

## Phase 2 — Clean the evidence pools (discipline)

Scope doc: `EVIDENCE_POOLS_DISCIPLINE.md`.

- [x] Formally scope and label the four pools — manual Guard, wave shadow, incubation, entry-intelligence — as **separate** evidence, never one edge story; no cross-pool pooling in any analysis. **Done =** each pool's scope/purpose documented and enforced.

## Phase 3 — Labelable outcome basis (A2)

Stage A closed as **A2**: the single-option executable-bid basis is not reliably viable (`STAGE_A_OUTCOME_BASIS_VIABILITY.md`).
Pre-reg spec: `A2_OUTCOME_BASIS_SPEC.md`.

- [x] **Pre-register the A2 alternative outcome basis.** Recommendation: **underlying-forward-return** over greeks-mapped (0DTE gamma/theta make the greeks map inaccurate on exactly the big moves). **Done =** frozen A2 basis spec with bias controls.
- [x] **Build/validate the A2 measurement** — `a2_measurement.py` produces signed 5/15/30/60-minute labels from provider-time, two-sided SPY quote mids, preserving endpoint timestamps, source provenance, missingness, and one-observation-per-episode-key. Built + tested.
- [x] **Cross-side collision remediation** — legacy co-timed CALL/PUT pairs traced to the pre-fix `has_reference → admit_episode` path (all M1). Fixed: `setup.qualified` admission gate, fail-closed `INTEGRITY_CROSS_SIDE_DOUBLE_QUALIFICATION` guard, quarantine manifest (data preserved, marked ineligible), A2 excludes quarantined IDs by default. Tests real (`pytest` in `.venv`). See `A2_COLLISION_REMEDIATION_DIRECTIVE.md`.
- [ ] **Operational gate — restart collector on v1.2** (EOD, flat-account) → verify runtime `v1.2` → post-restart collision scan = 0. See `COLLECTOR_V1.2_RESTART_RUNBOOK.md`. **Until done, no clean prospective episodes accrue.**

## Phase 4 — The MVP question: does any signal have edge?

- Edge-test spec: `REVERSAL_PHASE4_EDGE_TEST_SPEC.md`.
- Part 0 determination: threshold provenance is **UNPROVEN** (`A2_EXPLORATORY_HISTORICAL_RUN_DIRECTIVE.md`), so historical reconstruction is **exploratory / non-inferential only**. The inferential verdict comes solely from prospective data the thresholds never saw. Two tracks:

- [ ] **Exploratory historical run (fast, non-inferential).** Frozen detector over historical SPY 5-min bars → A2 labels → full harness, stamped `EXPLORATORY — NON-INFERENTIAL`. **Done =** episode count + descriptive stats (mean signed 60m return, null p, per-fold signs) reported as pipeline validation + directional smell test. **This does NOT close the box.**
- [ ] **Inferential edge test (prospective, the real verdict).** Accrue ≥200 non-overlapping episodes on the now-fixed collector (fresh data the thresholds never saw), then run the frozen harness → honest EDGE / NO EDGE / UNDERPOWERED. **Done =** verdict on out-of-sample prospective data. ~1yr at the v1 fire rate.
- [ ] **Branch on the result:**
  - **UNDERPOWERED / INCONCLUSIVE** → collect more episodes until the 200-episode validity gate is satisfied; do not call no-edge yet.
  - **No edge** → a real result. Pivot effort to the denser-signal order-flow studies (dealer pressure / CVD), not more reversal variants.
  - **Edge** → proceed to reversal v2 direction (new frozen cohort, *smallest* relaxation of `momentum_slowing` / `causal_confirmation`), liquidity-first contract selection, and cost-adjusted expectancy — per `REVERSAL_V2_DESIGN_BRIEF.md`.

---

## Parallel / standing research tracks (not on the MVP critical path)

- **Dealer Pressure Advisory Study** (`DEALER_PRESSURE_ADVISORY_STUDY_SPEC.md`) — parked after the optional UW subscription was canceled on 2026-08-29.
- **IVolatility research annex** (`IVOLATILITY_RESEARCH_ANNEX_PROPOSAL.md`) — parked after the optional IVolatility subscription was canceled on 2026-08-29.
- **CVD-Lab** (separate repo) — parked after the optional Databento subscription was canceled on 2026-08-29; prior detector v2 remains below the 100-episode floor (96).
- **Regime Layer v0** (`REGIME_LAYER_SPEC_v0.md`) — deterministic trend/range/high-vol tagger (efficiency ratio + ATR percentile), causal/point-in-time, advisory-only, no admission authority. Built + tested; attaches `regime_tag` to episodes. Stage-1a viability report built (`regime_distribution_v0.py`, currently `INSUFFICIENT_TAGGING` on the empty clean set). Conditions on the v1.2 clean stream; Stage-2 gated on Stage-1a = `VIABLE`.

## Ordering rationale

Phase 1 is the only phase that improves realized outcomes **today** (and needs no edge assumption). Phases 3–4 are what determine whether there is anything real to build on at all. Everything else waits on the Phase-4 verdict.
