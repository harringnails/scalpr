# Dry-Run Remediation Punch List — 2026-08-13

**Status:** remediation directive from the NO-GO audit. Paper/shadow. **Do not lock cohorts.** NO-GO stands until P0 clears.

## Root cause (read first)

Most findings are one thing: **the stale v1.1 runtime, up since Aug 11.** It explains the version mismatch, the two unqualified admissions, and the session-date bug (the process froze its session context on Aug 11, so today's 780 episodes are stamped `session_date: 2026-08-11` → 778 duplicate-reference rejections). That session-date bug also reveals the collector expects a **daily restart that was never automated** — so a manual restart fixes today but the problem recurs unless the automation is fixed. These are linked; fix them together.

Separately: **`post_close_audit.py` is itself buggy** (counts every evaluation as admitted; can't date outcomes). It produced some of the alarming numbers, so treat raw audit counts with suspicion until it's fixed.

## How to run this (the loop)

For every item below: **do what you can autonomously in your shell; for anything you cannot do (server restart, killing Mac processes, Keychain, cohort lock, localhost), STOP and report the exact command/steps and what success looks like — do not guess, do not mark it done, do not lock anything.** If an item is ambiguous or you need a decision from me, ask a specific question rather than picking a default.

Owner tags: **[CHAD]** operator-only · **[CODEX]** autonomous · **[DECISION]** needs my call.

---

## P0 — Keystone (unblocks the dry-run)

1. **[CHAD] EOD v1.2 restart** via `restart.sh` (the strong script — has the collector flag + flat-account proof; NOT `restart_cron.sh`), per `COLLECTOR_V1.2_RESTART_RUNBOOK.md`. Account is flat now. **Success =** runtime status reports `entry-bid-collector-v1.2`.
2. **[CODEX] Verify v1.2 rolls `session_date` across days.** Confirm the session-date bug is a stale-runtime artifact, not a latent v1.2 defect — i.e., that a v1.2 process running past midnight stamps the correct session_date. If v1.2 *also* relies on a daily restart, say so explicitly. If v1.2 has the bug in code, fix it before the restart is meaningful.
3. **[CHAD+CODEX] Post-restart verification:** re-sweep quarantine for today's v1.1 contamination (the 2 admissions + the 778 mis-stamped rows); confirm runtime `v1.2`, correct `session_date`, and post-restart collision scan = 0. **[CHAD]** runs the restart; **[CODEX]** confirms the ledger afterward.

## P1 — Correctness / trust (Codex autonomous; operator reviews before anything live)

4. **[CODEX] Fix `post_close_audit.py`** — stop counting evaluations as admitted; date outcome rows correctly (not via `evaluated_at`). This tool feeds GO/NO-GO; its bugs have been polluting the numbers. **High priority.**
5. **[CODEX] Wire the discretionary logger** per `DISCRETIONARY_OVERRIDE_CAPTURE_INTEGRATION_DIRECTIVE.md` — 17 paper exits today went unlogged; the pool file doesn't exist. Auto-capture TOOK at manual fill; fail-safe side-effect.
6. **[CODEX] Fix IVOL "COMPLETE with zero records"** (scalp_server.py:1150) — retry/flag instead of marking the date complete on an empty pull (silent success masking a failed fetch).
7. **[CODEX/DECISION] Reconcile restart automation.** `restart_cron.sh` omits the Entry Intelligence / UW / IVol / Cloud SQL flags and skips the open-order/flat-account check; `restart.sh` is the safe one. Either bring `restart_cron.sh` up to `restart.sh`'s safety **or** disable cron restart until it is safe — **and** decide with me whether daily auto-restart should be enabled at all (it's the durable fix for the session-date recurrence). Do not enable any auto-restart without my sign-off.
8. **[CODEX] Test environment.** Install FastAPI/Uvicorn in `.venv`; fix the test files that globally mutate cwd/import state so the full suite runs green. 87 pass independently, but "green" must mean the whole suite, or it's not a trust anchor.

## P2 — Hygiene

9. **[CHAD] Kill the stale Cloud SQL mirror processes** (the Aug 9 orphan + the second live one); **[CODEX]** make the mirror status fail-closed — never report "no backlog" when the status is stale (it hasn't updated since Aug 12, so the current no-backlog result is unreliable).
10. **[CHAD/WATCH] Disk free 17%.** Plan rotation/cleanup of the large `*.jsonl` / `*.csv` capture files before it bites a write.

## Discipline

- No cohort lock, no parameter changes, no trading, no auto-restart, until P0 clears and I sign off.
- Operator-only actions surfaced with exact commands; nothing marked done that wasn't actually done.
- The formal feed-qualification gate remains failed/provisional; today is operational-QA only, not research or predictive evidence.
