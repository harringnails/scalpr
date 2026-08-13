# Scalpr — Migration Handoff (to Codex)

Generated at handoff. This is the authoritative pointer document for migrating Scalpr.
Everything referenced below lives in this folder: `/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7`.

---

## 1. Metrics handoff — RAW files, not just conclusions

All raw data is already in this folder (nothing is locked in my head — take the files):

| File | What it is |
|---|---|
| `scalp_journal.csv` | Every trade (178+): entry/exit, peak_pct, realized_pct, reason, entry/exit market-context snapshot. **The primary metrics source.** |
| `tick_log.csv` | Standing SPY tick log (underlying only — no per-option paths). |
| `scalpr_intel_feature_snapshots_v0.jsonl` | Frozen decision-time feature records (`scalpr-intel-v0`). |
| `wave_observations_v0.jsonl`, `wave_exits_v0.jsonl` | Wave Riding shadow observations + exits. |
| `entry_policy_prototype_v0_{decisions,log,outcomes}.jsonl` | Entry-policy prototype traces. |
| `incubation_*/` folders | Incubation capture: `incubation_trades/`, `incubation_snapshots/`, `incubation_paths/`, `incubation_telemetry/`. |
| `wave_positions/`, `wave_seeds/`, `wave_obs_streams/` | Wave sim state + reproducible seeds. |
| `research_snapshots/`, `premarket_shadow/`, `runs/` | Daily immutable snapshots, premarket shadow, cached UW workups. |

**Conclusions already written up** (read these for interpretation, but the CSV/JSONL above are ground truth):
`COHORT_A_REPORT.md`, `COHORT_A_FINDINGS.md`, `INTEL_VALIDATION_REPORT.md`,
`ENTRY_INCUBATION_STUDY_v0.md`, `RATCHET_NOISE_ASSESSMENT.md`, `SCALPR_OVERVIEW.md` (version register).

Key empirical findings to carry forward:
- ~178 trades, ~43.6% win rate, **net-negative overall** — driven by reward asymmetry (cutting winners short / letting losers run), consistent with the FXCM "Traits of Successful Traders" study.
- **2026-07-31** (best recent day): +8.6% mean / 80% win; excluding one forgotten −95.7% trade → **+20.2% mean / 89% win**. Every trade that *developed* (peak ≥15%) won (+26.5% mean). The winners were captured by the **automated hold (stall + ratchet)**, not manual disengaging.
- Wave Riding Cohort A: **0 additions across 33 sims** — the config never "rides" (reversal trips before the add-trigger fires). Robust null result at 33/33.

---

## 2. Is the supplied archive the latest authoritative code?

**No archive is authoritative — THIS live folder is.** There is a `Scalpr7 Compressed/` subfolder (an older snapshot). Before trusting any zip, compare mtimes: the loose `.py` files in the root of `Scalpr7/` are the newest work (they include `guard_events.py` and `restart_cron.sh` added at the very end). **Migrate from the root folder, not from `Scalpr7 Compressed/`.**

## 3. Prior Git repo / commit history / other Scalpr folders

- **There is NO git repository.** `git log` returns nothing — the project has never been version-controlled. There is no commit history to hand over. **First action in Codex: `git init` and commit the current state as your baseline.**
- Only sibling folder inside the mount is `Scalpr7 Compressed/` (older). I cannot see any folders outside this mounted directory — if you have other Scalpr copies elsewhere on your Mac, I never had access to them, so check manually.

## 4. Current process status (running? open positions?)

**I cannot observe your Mac's live process or your Alpaca account from my sandbox** — my shell is an isolated Linux VM, not your machine, and can't reach `localhost:8420`. What I can say:
- No `scalpr_server.log` exists yet in the folder (the manual `restart.sh` logs to the Terminal, not a file), so I have no on-disk signal of a running server.
- **You must confirm directly:** check the dashboard at `http://localhost:8420`, and check open positions in the **Alpaca dashboard**. There were 3 paused/stuck guarded positions discussed earlier that were never confirmed closed — **verify those before migrating.**

## 5. Should V2 begin paper/shadow only?

**Yes — unequivocally. Begin V2 paper/shadow ONLY.** Reasons:
- The system is net-negative and unvalidated; nothing has earned live capital.
- Every cohort (Wave, Incubation) is explicitly `formal_cohort_eligible: false` / shadow-only by design.
- **Security:** `scalp.keys` is still present in this folder and was exposed earlier — **rotate the Alpaca keys before Codex touches anything, and never copy `scalp.keys` into the new repo.** `.gitignore` already lists `scalp.keys`, `*.keys`, `.env*`, `research_snapshots/`, `tick_log.csv`, `*.db` — replicate that in Codex before the first commit.

## 6. Alpaca subscriptions / feeds

**I don't have your account info and can't confirm your subscription tier — do not send credentials; verify these in your Alpaca dashboard.** What the *code* uses:
- **Underlying quotes/bars:** Alpaca **IEX** feed.
- **Options quotes:** Alpaca **OPRA**.
- Feeds are kept field-separated with independent timestamps + a skew gate (never merged).
- **Known limitations observed:** rate-limit **429s** under the shadow observers (Wave + Incubation both fetch per-position/per-trade, unbatched). **SIP** is used only in `feed_quality.py` cross-feed validation and requires a paid subscription — confirm whether your account has it.

## 7. Frozen thresholds / cohorts / policies — MUST NOT CHANGE

Changing any of these forces a NEW cohort (invalidates comparability):
- **Incubation config hash** `3425809bb0a3200fef8439dbcd5791d877cf4fe9e998c5f0cc41f8b100007eed` (`incubation-record-v0.1-study-role`).
- **Incubation primary cells:** baseline `CURRENT × None`; candidate `TIME_OR_PROFIT × None`; safety-overlay `TIME_OR_PROFIT × HARD_STOP_15`.
- **Materiality** 10 / 10 / 15 pp on **executable option bid**; recovery window 15 min. Hard-stop scenarios: `None / 15 / 12.5 / 10`.
- **Feature schema** `scalpr-intel-v0`; quote staleness thresholds **900s warn / 3600s unlabelable**.
- **Wave Riding** `wave-riding-v0`, `intraday-atr-v0` (5-min ATR), baseline `wave-riding-baseline-v0`.
- **Flow-evidence** `flow-evidence-v0`: GREEN requires fresh + ≥5 in-band contracts + ≥3 agreeing signals + **0 opposing**.
- Live **Guard** has **no separate hard stop** — only the ratchet ladder (`CURRENT_CONFIGURED_HARD_STOP = None`). This is a fact V2 must preserve or explicitly version.

## 8. Initial asset scope — CONFIRM (currently in code)

Current code assumes: **SPY / QQQ / IWM** for workups; **options** (0DTE allowed); the tick logger is **SPY-only**. Recommended V2 start: **SPY only, options, 0–2 DTE**, then widen. *This is your call — please confirm the target scope for Codex.*

## 9. Deployment preference — CONFIRM

Currently **local Mac** (`python3 scalp_server.py`, `localhost:8420`). Options for V2: local Mac (simplest, but only runs when your Mac is on), always-on hosted (survives closes/restarts, better for shadow capture + scheduled restarts), or both. *Your call — tell Codex which.*

## 10. Workflows to retain (walkthrough)

I can't screenshot your Mac from here, but the workflows worth keeping:
1. **Dashboard** (`dashboard.html` @ `localhost:8420`): holdings + Guard status, contract picker with 🟢★ flow-evidence stars, Wave Riding panel + Cohort A progress, Options-Flow Evidence panel, premarket scorecard.
2. **Standard mode ratchet Guard** (`scalp_server.py` Guard class) — the core live paper mechanic.
3. **Workup** (`workup.py` / `workup_api.py`): UW options-flow → per-contract in-band read (now includes GEX + dark pool context).
4. **Shadow loops:** Wave observer + Incubation observer (gated by `WAVE_RIDING_ENABLED`, `INCUBATION_SHADOW_ENABLED`).
5. **New (this session):** `guard_events.py` — persists disengage/re-engage events (currently only printed to terminal); wire `log_event("pause"/"resume", guard)` into the pause/resume endpoints in `scalp_server.py` (not yet wired — do this in V2).

---

### Immediate migration checklist
1. **Rotate Alpaca keys now**; do not copy `scalp.keys` into Codex.
2. `git init` in the root `Scalpr7/` folder; add the `.gitignore` (already present) before first commit.
3. Confirm no open/stuck positions (dashboard + Alpaca).
4. Copy raw data files (§1) as read-only reference.
5. Keep V2 **paper/shadow only**; preserve the frozen items in §7.
6. Confirm §8 scope and §9 deployment with your own decision.
