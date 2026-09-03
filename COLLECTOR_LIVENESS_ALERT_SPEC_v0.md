# Collector Production Liveness Alert — Spec v0

**Status:** do-now safety monitor. **Read-only. No writes to any collector/evidence file, no execution/Guard authority, no server changes.** Standalone script — works on the current rolled-back code (does not depend on Phase-1 `/health` endpoints). Its job: catch a dark or non-producing collector **within the session**, not days later on a post-close audit.

## Why (the failure it prevents)

Twice the collector went dark for multiple sessions before anyone noticed — the Phase-1 credential crash and the non-evaluation bug. In both, the **process was alive but not producing records**. So "is the process up" is the wrong test. The right test is: **is the collector writing decision records during market hours?**

## What it checks (market-aware)

Read, per poll:
- `entry_intelligence_collector_status_v1.json` → `state`, `market_window`, `updated_at` (mtime).
- Newest write timestamp across `entry_intelligence_decisions_v1.jsonl` / `entry_intelligence_decision_events_v1.jsonl` / `entry_intelligence_gate_results_v1.jsonl` (file mtime is sufficient; no parsing needed).

Decision logic:
- **During RTH** (`state == ACTIVE_RTH_CAPTURE` or `market_window == true`): if **no decision-file write in the last 5 minutes** → `ALERT: collector not producing during RTH`. (Normal RTH cadence is ~1 write/minute, so 5 min stale is unambiguous.)
- **Market closed** (`ARMED_MARKET_CLOSED`): decision files are *expected* to be idle. A collector status `updated_at` older than 5 minutes is still logged as `ALERT: collector heartbeat stale`, but it is notification-suppressed outside the operational window.
- **Pre-open / RTH**: from 30 minutes before the regular 09:30 ET open through 16:00 ET on weekdays, stale `ARMED_MARKET_CLOSED` status notifies. This ensures an overnight log-only incident escalates before it can threaten session capture. The status-driven RTH no-production alert remains unchanged.
- If the status file is missing/unparseable for > 5 min → `ALERT: collector status unreadable`.

A-priori thresholds (conservative, not tuned): `RTH_STALE = 5 min`, `HEARTBEAT_STALE = 5 min`, poll every `60s`. These are round operational values, changeable by the operator.

## How it surfaces (no paging infra needed)

On any ALERT:
1. Append a line to a dedicated `collector_alerts_v0.log` (timestamp, alert type, the stale age).
2. Fire a macOS notification unless this is benign off-hours `ARMED_MARKET_CLOSED` heartbeat staleness. That case remains log-only; if it persists into pre-open, it notifies immediately.
3. Optional: also print to stdout so it's visible if run in a terminal.

De-dupe: don't re-notify every 60s for the same ongoing condition — notify on transition to ALERT, then at most once every ~15 min while it persists; notify once on recovery (`RECOVERED`).

## How it runs (survives reboots, zero manual effort)

Package as a small standalone Python script (`collector_liveness_alert.py`) with **no imports beyond stdlib** (json, os, time, subprocess, datetime). Run it one of two ways:
- **launchd agent** (preferred) — a `~/Library/LaunchAgents/…plist` that keeps it running and restarts it on login/reboot. This is what makes it *automatic* — you stop having to run daily checks by hand.
- Or a simple `nohup python3 collector_liveness_alert.py &` loop for now.

## Non-negotiables

- **Read-only.** Opens files for reading only; never writes to collector/evidence data; no order/Guard/execution path; no server process interaction.
- Standalone; no dependency on the (shelved) Phase-1 health endpoints — reads the status/evidence files directly, so it works on the current known-good code.
- Market-aware: never alerts on idle decision files when the market is closed (that's expected).
- Thresholds a-priori and operator-visible; no tuning against outcomes.

## Tests (run in `.venv`)

- RTH + recent write → no alert.
- RTH + no write for > 5 min → ALERT (collector dark) — this is the Aug-21/Aug-24 failure it must catch.
- Market closed + idle decision files but fresh status heartbeat → no alert.
- Market closed + status heartbeat > 5 min stale → logged ALERT, no notification.
- Pre-open + status heartbeat > 5 min stale → logged ALERT and notification.
- Missing/unparseable status file → ALERT.
- De-dupe: one notification on transition, not one per poll; one RECOVERED notification.

## What it is NOT

- Not a fix for the dark-collector *cause* (that's dense-source / Phase-1) — it's the *detector*, so a future regression pages you in 5 minutes instead of 2 days.
- Not a trading/authorization component.
