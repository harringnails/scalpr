"""Fixture tests for the read-only Scalpr status renderer."""

from __future__ import annotations

import ast
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import scalpr_status as status


NOW = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_tree(tmp_path):
    root = tmp_path / "Scalpr7"
    pin = tmp_path / "Scalpr7-flashalpha-pin-scanner"
    databento = tmp_path / "Scalpr7-a2-relabel" / "a2_exploratory_databento"
    write_json(root / "v2_data/a2_measurement/a2_summary_dense_v0.json", {
        "clean_a2_eligible_episode_count": 12,
        "data_integrity_status": "PASS",
        "phase4_preflight": "UNDERPOWERED_INCONCLUSIVE",
        "mean_signed_return_60m": -0.0001,
    })
    write_json(root / "v2_data/a2_measurement/a2_accrual_status_v0.json", {
        "gate": {"non_overlapping_count": 12, "target": 200, "remaining": 188, "reached": False},
        "accrual_window": {"as_of_trading_session": "2026-08-31"},
    })
    write_json(root / "entry_intelligence_collector_status_v1.json", {
        "state": "ACTIVE_RTH_CAPTURE",
        "collector_version": "collector-v1.2",
        "updated_at": "2026-09-01T13:59:30Z",
        "execution_authority": False,
        "guard_access": False,
        "cohorts_locked": True,
        "collection_role": "LOCKED_FORWARD",
        "last_session_counts": {"episodes": 3, "no_trade": 2},
    })
    append_jsonl(root / "collector_alerts_v0.log", {
        "event": "RECOVERED", "kind": "ok", "state": "ACTIVE_RTH_CAPTURE",
        "message": "OK: collector producing", "ts": "2026-09-01T13:59:45Z",
    })
    append_jsonl(pin / "flashalpha_pin_study_v0.jsonl", {
        "record_type": "PIN_CANDIDATE", "session_date": "2026-08-31",
        "observed_at_utc": "2026-08-31T18:00:00Z", "grade": "LOW_PIN_PRESSURE",
        "evidence": {"spot": 599, "pin_score": 35, "gamma_regime": "positive",
                     "pocket": {"put_wall": 595, "call_wall": 605, "spot_inside": True}},
    })
    append_jsonl(pin / "flashalpha_pin_study_v0.jsonl", {
        "record_type": "PIN_CANDIDATE", "session_date": "2026-09-01",
        "observed_at_utc": "2026-09-01T13:55:00Z", "grade": "HIGH_PIN_PRESSURE",
        "evidence": {"spot": 600, "pin_score": 82, "gamma_regime": "positive",
                     "pocket": {"put_wall": 595, "call_wall": 605, "spot_inside": True}},
    })
    append_jsonl(pin / "flashalpha_pin_study_v0.jsonl", {
        "record_type": "PIN_SESSION_OUTCOME", "session_date": "2026-08-31",
        "status": "AVAILABLE", "close_near_magnet": True,
    })
    for day, pnl in (("2026-08-28", 90.0), ("2026-08-31", -110.0), ("2026-09-01", 30.0)):
        append_jsonl(pin / "flashalpha_pin_ic_study_v0.jsonl", {
            "record_type": "IC_SETTLEMENT", "session_date": day,
            "status": "AVAILABLE", "pnl": {"net_pnl_dollars": pnl},
        })
    run = databento / "full-history-test"
    write_json(run / "cost_projection.json", {
        "episode_count": 2484, "quote_window_count": 12091,
        "cost_probe_completed": True,
    })
    write_json(run / "cost_probe.json", {
        "sample_window_count": 20, "total_window_count": 12091,
        "actual_probe_cost_usd": 0.03,
        "projected_total_quote_cost_usd": 18.25,
        "projected_total_quote_records": 14000000,
        "is_inferential": False,
    })
    return root, pin, databento


def test_complete_render_preserves_badges_and_descriptive_values(tmp_path):
    root, pin, databento = fixture_tree(tmp_path)
    output = tmp_path / "scalpr_status.html"
    result = status.render(root=root, pin_root=pin, databento_root=databento, output=output, now=NOW)
    page = output.read_text(encoding="utf-8")
    assert result["execution_authority"] is False
    assert result["read_only_sources"] is True
    assert page.count(status.INFERENTIAL_BADGE) == 1
    assert page.count(status.SHADOW_BADGE) == 3
    assert "12<small> / 200 CLEAN EPISODES" in page
    assert "188 remaining" in page
    assert "HIGH_PIN_PRESSURE" in page
    assert "82.00" in page
    assert "1/20" in page
    assert "3<small> / 60 SETTLED DAYS" in page
    assert "$-110.00" in page
    assert "$3.33" in page
    assert "2,484" in page
    assert "$18.25" in page
    assert "not a trade signal" in page
    assert "execution authority: false" in page


def test_missing_sources_render_not_yet_without_failure(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    output = tmp_path / "missing.html"
    status.render(root=root, output=output, now=NOW)
    page = output.read_text(encoding="utf-8")
    assert "not yet" in page
    assert "—" in page
    assert status.INFERENTIAL_BADGE in page
    assert status.SHADOW_BADGE in page


def test_render_does_not_modify_any_source(tmp_path):
    root, pin, databento = fixture_tree(tmp_path)
    sources = [path for base in (root, pin, databento) for path in base.rglob("*") if path.is_file()]
    before = {path: digest(path) for path in sources}
    status.render(
        root=root, pin_root=pin, databento_root=databento,
        output=tmp_path / "status.html", now=NOW,
    )
    assert {path: digest(path) for path in sources} == before


def test_ledger_text_is_html_escaped(tmp_path):
    root = tmp_path / "root"
    write_json(root / "entry_intelligence_collector_status_v1.json", {
        "state": "<script>alert(1)</script>", "updated_at": NOW.isoformat(),
    })
    output = tmp_path / "escaped.html"
    status.render(root=root, output=output, now=NOW)
    page = output.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_collector_heartbeat_age_and_control_states(tmp_path):
    root, pin, databento = fixture_tree(tmp_path)
    model = status.gather_status(
        status.discover_sources(root, pin_root=pin, databento_root=databento), now=NOW,
    )
    assert model["collector"]["heartbeat_age"] == 30
    assert model["collector"]["execution_authority"] is False
    assert model["collector"]["guard_access"] is False
    assert model["collector"]["cohorts_locked"] is True


def test_latest_databento_probe_is_selected(tmp_path):
    root, pin, databento = fixture_tree(tmp_path)
    older = databento / "older" / "cost_probe.json"
    write_json(older, {"actual_probe_cost_usd": 99, "sample_window_count": 2})
    os.utime(older, (1, 1))
    model = status.databento_model(status.Sources(root, pin, databento))
    assert model["actual_probe_cost"] == 0.03
    assert model["sample_windows"] == 20


def test_module_has_no_server_order_or_admission_imports():
    source = Path(status.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported.isdisjoint({"scalp_server", "alpaca", "entry_policy", "wave_server"})
    assert "submit_order" not in source
    assert 'execution_authority": True' not in source


def test_atomic_writer_leaves_no_temporary_file(tmp_path):
    output = tmp_path / "status.html"
    status.write_html_atomic(output, "first")
    status.write_html_atomic(output, "second")
    assert output.read_text(encoding="utf-8") == "second"
    assert list(tmp_path.glob(".status.html.*.tmp")) == []
