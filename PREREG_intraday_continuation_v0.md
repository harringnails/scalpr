# Pre-Registration — Intraday Continuation Study (v0)

**Study ID:** `intraday_continuation_v0`
**Status:** FROZEN pre-registration. Paper/shadow, observational, **NON-INFERENTIAL** until the sample target is met and the verdict rule fires. No admission, no Guard, no order authority. Nothing here authorizes a trade.
**FROZEN / operator-approved:** 2026-09-04. This file is SHA-256 stamped into every episode record and version-locked by its freeze commit.
**Prospective accrual begins:** the **next full RTH session after the 2026-09-04 freeze commit.** Every session on or before 2026-09-04 is **in-sample / exploratory** and excluded from the inferential count.
**Mutation rule:** no parameter may change after this commit. Any change requires a new study ID and resets N to zero.

---

## 1. Motivation (honest, outcome-independent)

The most concrete point-in-time sequence around the 2026-09-03 rally was: a high-convexity 0DTE call (delta ≈ 0.6, gamma ≈ 0.15 per FlashAlpha 5-min strike snapshot), a sharp liquidity **sweep** ~10:56 ET, a rapid **reclaim**, three consecutive positive SPY minutes, a **VWAP reclaim**, spot crossing the **769 call wall**, and the call wall **migrating** 769→770→771→772→773→774. This is a plausible *breakout/continuation* signature — but it is described from one winning trade with the outcome known, and the "first marker at 11:01 left +142%" figure is an **in-sample** simulation on that single winner, not evidence. This study freezes the sequence as a testable rule and accrues it forward.

## 2. The frozen hypothesis (H2)

> When, during RTH, SPY exhibits the **sweep-reclaim + acceptance + wall-migration** signature defined in §4, the session **continues directionally in the reclaim direction** by more than a session-time-and-volatility-matched control, on the underlying forward-return basis in §5.

**Direction rule:** tested direction = sign of the reclaim (down-sweep then up-reclaim → **long** arm; up-spike then down-reclaim → a separate symmetric **short** arm, pre-registered on its own). v0 covers the **long** arm only.

**Null (H0):** conditional on the same session-time block and realized-vol bucket, signature episodes show **no** forward-return advantage over matched non-signature windows.

## 3. Marker set — frozen, with a hard dependency note

All markers are computed **point-in-time from data we actually capture.** A marker we cannot yet measure reliably is **excluded from v0** and may only be added by opening a new study ID.

| # | Marker | Source (current capture) | Status in v0 |
|---|--------|--------------------------|--------------|
| 1 | Sweep: SPY mid trades `≥ S` pts beyond a local extreme, then | `tick_log.csv` mid | **IN** |
| 2 | Reclaim within `≤ R` seconds back through the extreme | `tick_log.csv` mid | **IN** |
| 3 | Acceptance: `≥ 3` consecutive positive 1-min underlying returns | `tick_log.csv` mid | **IN** |
| 4 | VWAP reclaim + positive VWAP slope over `≥ V` min | computed from `tick_log` mid (see §3a) | **IN (caveated)** |
| 5 | Spot crosses the nearest call wall | FlashAlpha `levels`/`zero_dte` | **IN** |
| 6 | Call wall migrates upward on the next 5-min read | FlashAlpha 5-min strike snapshots | **IN** |
| 7 | Expanding **executed** volume | **NOT CAPTURED** — see §3b | **EXCLUDED from v0** |

**Frozen thresholds:** `S = 0.25` pts, `R = 180 s`, `V = 2 min`, sweep/reclaim confirmed on non-crossed quotes only. Acceptance remains `≥ 3` consecutive positive 1-minute underlying returns. Executed volume is **EXCLUDED**.

### 3a. VWAP caveat
Our `tick_log` records **quote mid**, not trade prints, so "VWAP" here is a **quote-mid VWAP proxy**, stamped as such. It is retained because it is computable point-in-time and self-consistent, but it is **not** a true traded-VWAP. If a traded-VWAP source is later added, that is a **new** study, not an edit to this one.

### 3b. Executed-volume prerequisite (the reason volume is excluded)
`tick_log` carries `bid_size`/`ask_size` (displayed quote sizes), **not executed trade volume**; Scalpr `relative_volume` was `null`; FlashAlpha total-volume and per-strike `768C` volume fields **did not update** through much of the 2026-09-03 move before later rebasing — so they cannot be trusted as 5-min flow acceleration. **"Expanding volume" is therefore NOT a condition in v0.** It may become one **only** after a reliable point-in-time **executed-trade-volume** source is added and validated — at which point a successor study (`intraday_continuation_v1`) is pre-registered fresh. Adding volume mid-accrual voids the study.

## 4. Episode definition (point-in-time, no look-ahead)

- **Trigger:** markers fire in this strict order: sweep → reclaim within `R` → three-positive-minute acceptance → proxy-VWAP reclaim with positive `V`-window slope → spot crosses the nearest call wall.
- **Confirmation:** marker 6 (wall migration up) on the **next** 5-min FlashAlpha read after the wall cross. Because this looks one read forward, **t0 (the anchor for forward returns) is set at the confirmation read**, so no outcome is measured before the full signature (including migration) is in hand — no look-ahead.
- **One episode per session** (first qualifying signature). Non-overlap automatic. Missing/stale inputs → excluded, logged, never imputed.

## 5. Outcome basis (identical to the A2 dense basis)

- SPY **underlying forward return** from t0, dense Alpaca SIP mids, `≤ 5 s` freshness, non-crossed, horizons **5 / 15 / 30 / 60 min**, all required. Missing horizon → `A2-UNAVAILABLE`, excluded.
- Long arm scores **+** for up-continuation. MFE/MAE/time-to-MFE recorded descriptively. Option P&L is **not** the inferential basis.

## 6. Guard operability — measured SEPARATELY, NOT mixed into the signal verdict

Same structure as the companion study: this study's verdict (§8) is a **pure signal test on the underlying basis** and contains no Guard result. Guard operability is measured by the separate **Guard Counterfactual study** (`PREREG_guard_operability_counterfactual_v0.md`), which replays every candidate with the Guard **continuously enabled** and reports exit time, realized return, MFE, MAE, and opportunity cost. **Runnability** = signal `EDGE` **and** Guard-compatible, decided as a joint interpretation after both complete. A signal `EDGE` with an incompatible Guard means **fix the exit policy, do not discard the marker**. (Rationale: the generating trade only won with the Guard paused through a −34.9% drawdown — an exit-policy fact that must not be allowed to invalidate a real marker.)

## 7. Freshness, provenance, exclusions

- All reads timestamp+age stamped; stale at a boundary → excluded, logged, never imputed.
- Historical/Databento = **EXPLORATORY / NON-INFERENTIAL**, cannot count toward N.
- 2026-09-03 and all pre-lock sessions = **in-sample**, excluded from N.

## 8. Sample, null, walk-forward, verdict

- **Target N:** ≥ **150** non-overlapping qualifying episodes. Below → **`UNDERPOWERED`**.
- **Matched null:** same session-time block + realized-vol bucket, no signature; **p = (k + 1) / (n + 1)**.
- **Walk-forward:** 4-fold **chronological**; edge must hold out-of-fold.
- **Verdict (frozen, no loosening):**
  - **`EDGE`** — pooled effect positive at p ≤ 0.01, consistent sign in ≥ 3/4 folds (signal only; Guard reported separately per §6).
  - **`NO EDGE`** — N met, criteria unmet.
  - **`UNDERPOWERED`** — N below target.
  - _Runnability_ (`EDGE` **and** Guard-compatible) is a joint interpretation across this and the Guard study — not a verdict category here.

## 9. Freeze mechanics (non-negotiable)

- On lock: committed, SHA in the ledger header. No parameter (`S,R,V`, marker set, horizons, N, p, folds, verdict thresholds) may change post-lock. Any change → new study ID, N resets to zero. Loosening to reach a verdict voids the study.
- Isolated: ledger reads only; no execution/admission/collector/server/A2/dense wiring. `scalp_server.py` untouched.

## 10. Prerequisites before accrual can score

1. Point-in-time marker engine (sweep/reclaim/acceptance/proxy-VWAP/wall-cross) over `tick_log` + FlashAlpha 5-min reads, all freshness-stamped.
2. Wall-migration detector on consecutive 5-min FlashAlpha snapshots.
3. Always-active-Guard replay harness (shared with the companion study).
4. Episode logger `intraday_continuation_v0.jsonl` with full provenance + exclusion reasons.
5. **Deferred (not required for v0, required for any volume successor):** validated point-in-time executed-trade-volume source.

## 11. Tests (run in `.venv`)

- Point-in-time correctness: t0 set at the migration-confirmation read; no outcome/marker read crosses its boundary.
- Sequence integrity: markers must fire **in order** within windows; a reclaim before a valid sweep does not trigger.
- Volume exclusion: no code path lets a volume field enter the trigger in v0.
- Proxy-VWAP is labeled a quote-mid proxy in every record, never as traded-VWAP.
- Exclusion integrity: stale/missing FlashAlpha or SPY reads → excluded + logged.
- Signal/Guard separation and in-sample-exclusion tests as in the companion study (signal verdict references no Guard field).
