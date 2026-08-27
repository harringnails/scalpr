from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import collector_liveness_alert as alert


def _write_status(path: Path, *, state: str, market_window: bool, updated_at: float) -> None:
    path.write_text(
        json.dumps(
            {
                "state": state,
                "market_window": market_window,
                "updated_at": datetime.fromtimestamp(updated_at, tz=timezone.utc).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _make_decision_files(tmp_path: Path, *, mtime: float) -> tuple[Path, Path, Path]:
    decisions = tmp_path / "entry_intelligence_decisions_v1.jsonl"
    events = tmp_path / "entry_intelligence_decision_events_v1.jsonl"
    gates = tmp_path / "entry_intelligence_gate_results_v1.jsonl"
    for path in (decisions, events, gates):
        path.write_text("{}\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))
    return decisions, events, gates


def test_rth_recent_write_is_ok(tmp_path):
    now = 1_000.0
    status = tmp_path / "entry_intelligence_collector_status_v1.json"
    _write_status(status, state="ACTIVE_RTH_CAPTURE", market_window=True, updated_at=now)
    decisions, events, gates = _make_decision_files(tmp_path, mtime=now - 60)
    result = alert.evaluate_alert(
        status_path=status,
        decision_paths=(decisions, events, gates),
        now=now,
    )
    assert result["alert"] is False
    assert result["kind"] == "ok"


def test_rth_stale_write_alerts(tmp_path):
    now = 1_000.0
    status = tmp_path / "entry_intelligence_collector_status_v1.json"
    _write_status(status, state="ACTIVE_RTH_CAPTURE", market_window=True, updated_at=now)
    decisions, events, gates = _make_decision_files(tmp_path, mtime=now - 301)
    result = alert.evaluate_alert(
        status_path=status,
        decision_paths=(decisions, events, gates),
        now=now,
    )
    assert result["alert"] is True
    assert result["kind"] == "rth_no_production"
    assert result["message"] == "ALERT: collector not producing during RTH"


def test_market_closed_fresh_heartbeat_is_ok(tmp_path):
    now = 1_000.0
    status = tmp_path / "entry_intelligence_collector_status_v1.json"
    _write_status(status, state="ARMED_MARKET_CLOSED", market_window=False, updated_at=now - 120)
    result = alert.evaluate_alert(status_path=status, decision_paths=(), now=now)
    assert result["alert"] is False
    assert result["kind"] == "ok"


def test_market_closed_stale_heartbeat_alerts(tmp_path):
    now = 1_000.0
    status = tmp_path / "entry_intelligence_collector_status_v1.json"
    _write_status(status, state="ARMED_MARKET_CLOSED", market_window=False, updated_at=now - 301)
    result = alert.evaluate_alert(status_path=status, decision_paths=(), now=now)
    assert result["alert"] is True
    assert result["kind"] == "collector_heartbeat_stale"


def test_missing_status_alerts(tmp_path):
    status = tmp_path / "missing.json"
    result = alert.evaluate_alert(status_path=status, decision_paths=(), now=1_000.0)
    assert result["alert"] is True
    assert result["kind"] == "collector_status_unreadable"


def test_dedupe_transition_and_recovery(tmp_path, monkeypatch):
    now = 1_000.0
    status = tmp_path / "entry_intelligence_collector_status_v1.json"
    alert_log = tmp_path / "collector_alerts_v0.log"
    state_path = tmp_path / "collector_alerts_v0.state.json"
    decisions, events, gates = _make_decision_files(tmp_path, mtime=now - 400)
    _write_status(status, state="ACTIVE_RTH_CAPTURE", market_window=True, updated_at=now)
    monkeypatch.setattr(alert, "_notify", lambda *args, **kwargs: None)

    result1 = alert.run_once(
        status_path=status,
        decision_paths=(decisions, events, gates),
        alert_log_path=alert_log,
        state_path=state_path,
        now=now,
        notify=True,
    )
    assert result1["event"] == "ALERT"
    assert len(alert_log.read_text(encoding="utf-8").strip().splitlines()) == 1

    result2 = alert.run_once(
        status_path=status,
        decision_paths=(decisions, events, gates),
        alert_log_path=alert_log,
        state_path=state_path,
        now=now + 60,
        notify=True,
    )
    assert result2["event"] is None
    assert len(alert_log.read_text(encoding="utf-8").strip().splitlines()) == 1

    _write_status(status, state="ARMED_MARKET_CLOSED", market_window=False, updated_at=now + 60)
    result3 = alert.run_once(
        status_path=status,
        decision_paths=(),
        alert_log_path=alert_log,
        state_path=state_path,
        now=now + 120,
        notify=True,
    )
    assert result3["event"] == "RECOVERED"
    assert len(alert_log.read_text(encoding="utf-8").strip().splitlines()) == 2
