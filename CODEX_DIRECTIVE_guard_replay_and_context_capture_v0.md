# Codex Directive — Guard Replay Harness + Minimal Market-Context Capture (v0)

**Scope:** implement two isolated, unblocked deliverables from the frozen pre-registrations in this repo:
`PREREG_guard_operability_counterfactual_v0.md` (Study 3) and `PREREG_market_context_layer_v0.md` (Part 1 capture only).
Everything else in those specs — the context→profile mapping wiring, the five-way policy replay, and the Study A/B signal loggers — is **explicitly deferred** (see §Deferred).

---

## Safety perimeter (hard — overrides any convenience)

- **Paper/shadow, replay + capture ONLY.** No execution, admission, order, or Guard wiring. `execution_authority=false`, `guard_access=false` unchanged.
- **`scalp_server.py` must remain byte-identical.** Do not import these modules into it. Verify with a diff at the end.
- **No live-Guard modification.** The replay reads stored quotes and simulates exits offline. It must not touch, call, or alter the running Guard.
- **Isolation:** new modules read ledgers/quotes and write their own new ledger files only. No writes to A2, dense, collector, admission, or server state.
- **Keys in macOS Keychain only** (operator Terminal for any authenticated pull). Never hardcode/log/commit keys.
- **Repo/branch:** operative repo only, new branch based on real safety `8ea24267` (verify `git merge-base --is-ancestor 8ea24267 HEAD`). Do **not** build in a stray clone or fabricated base.
- **Done-bar (§2 of the operating contract):** real `.venv` `pytest` green + a behavior proof (actual output artifacts named below) + commit to the operative repo. Report commit SHA, branch, pass count, and the `scalp_server.py` diff status.

---

## Deliverable 1 — Guard Replay Harness (Study 3), run on today's trades

Implement a deterministic, replay-only Guard simulator per `PREREG_guard_operability_counterfactual_v0.md`.

**Baseline policy to replay (the shipped whole-position ratchet + stall):**
- Giveback ratchet: profit ≥ 0% → max giveback 15%; ≥ 15% → 6%; ≥ 30% → 2.5% (sell 100% of remaining in one order).
- Stall exit: 45 s above 20% profit; grace 60 s; confirm reads 2.

**Inputs:** the stored ~5 s option bid/ask series from the incubation path for each candidate.
**Fills:** exits fill at the **bid**; apply the frozen cost model ($0.05 slippage, $0.65/leg). No mid fills. No look-ahead — the exit decision at time *t* uses only quotes ≤ *t*.

**Run it against 2026-09-03's three paper trades (from `scalp_journal.csv` + `guard_events_v0.jsonl`), continuously guarded (NO pause):**
1. `SPY260903C00768000`, entry 1.52, ~10:22 ET (the +205.9% actual, which was guard-paused).
2. `SPY260903C00768000`, entry 4.49, ~12:51 ET.
3. `SPY260903P00773000`, entry 0.44, ~14:00 ET.

**Per-candidate metrics → `guard_operability_counterfactual_v0.jsonl`:** exit time, time-in-trade, always-active realized return (net costs), MFE, MAE, opportunity cost (MFE − realized), early-exit flag (exited while realized < 0 before continuation), and — for trade 1 — the always-active result **beside** the actual paused outcome, so the cost of the pause is explicit.

**Report in plain language:** for the +205.9% winner, **where would the always-active baseline Guard have exited** relative to the −34.9% drawdown, and what return would it have realized? (This is the direct test of the "Guard closes too quickly" hypothesis.)

**Tests (`.venv`):** determinism (same series → same exit, byte-stable); no look-ahead; baseline fidelity on hand fixtures (a +30% peak then −2.5% giveback exits; a 46 s stall above 20% exits); trade-1 early-exit flag computes against the −34.9% drawdown; harness reads no signal-verdict field.

## Deliverable 2 — Source probe + minimal Market-Context Shadow Ledger (capture only)

**Step 2a — Source-availability probe (report before building the sampler).** Determine, from the sources we already use (Alpaca SIP; FlashAlpha), which context fields are **actually feedable point-in-time**, and report a table of feedable vs not-available:
- Feedable-now candidates: SPY structure (VWAP proxy, opening-range break, realized vol, spread quality), ETF cross-asset (QQQ, IWM, sector ETFs), FlashAlpha options structure (gamma regime, flip, walls, 0DTE, toward-vs-through).
- Verify availability, do **not** assume: VIX index level, Treasury yields, ES futures, dollar index, full 500-constituent breadth, real-time news/catalysts.

**Step 2b — Build the isolated sampler** writing `market_context_shadow_v0.jsonl` every **30–60 s during RTH**, containing **only** the fields the probe confirmed feedable. Rules:
- Every field freshness-stamped (provider ts + age); mark `data_freshness` ∈ {CLEAN, DEGRADED, STALE}. Missing/stale → recorded as such, never imputed.
- Any breadth field built from a capturable **subset** of large-cap weights must be stamped `proxy` (key like `largecap_breadth_proxy`), **never** `sp500_breadth`.
- Point-in-time only; no field may use future data. Capture is forward-only.
- **Emit the state/sub-scores as recorded fields only — do NOT wire them to the Guard or any exit.** This is pure capture so data starts accruing now.

**Tests (`.venv`):** every record freshness-stamped; stale/missing fields recorded not imputed; proxy fields labeled `proxy`; no field uses future data; sampler writes only its own ledger and touches nothing else.

## Deferred (do NOT build in this directive)

- Context→exit-profile **mapping wiring** and the **five-way policy replay** (incl. the naive-wider-stop control) — wait until context data has accrued and a signal study shows something worth holding.
- **Study A/B signal episode loggers** — pending operator lock of the frozen parameters (`F` source, `A/W/G`, `S/R/V`, N, thresholds) in `PREREG_prior_regime_flip_reclaim_v0.md` and `PREREG_intraday_continuation_v0.md`.
- **Executed-volume**, full-500 breadth, ES futures, live yields, dollar, news wire — until a confirmed source exists.

## Report back

Commit SHA + branch; `.venv` pass count; `scalp_server.py` diff status (must be unchanged); the Deliverable-1 plain-language finding on the winner; and the Deliverable-2 source-availability table.
