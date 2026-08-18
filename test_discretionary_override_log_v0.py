import ast
import inspect
import json
from datetime import datetime, timezone

import discretionary_override_log_v0 as dol


def test_module_has_no_broker_order_or_guard_imports():
    tree = ast.parse(inspect.getsource(dol))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert not any(name.startswith(("alpaca", "scalp_server")) for name in imports)


def _bars(count=80):
    rows = []
    close = 100.0
    for bucket in range(count):
        close += 0.1
        rows.append({"bucket": bucket, "close": close, "high": close + 0.2, "low": close - 0.2})
    return rows


def _precheck_snapshot():
    return {
        "version": "precheck-weighted-evidence-v0",
        "decision": "YES",
        "agreement_pct": 71.4,
        "input_coverage_pct": 100.0,
        "horizons": {"tactical": {"coverage_pct": 100.0}},
    }


def test_four_cell_completeness_is_representable(tmp_path):
    base = dict(
        symbol="SPY",
        trade_type="option",
        implied_direction="bullish",
        decided_at=datetime(2026, 8, 13, 14, 30, tzinfo=timezone.utc),
        minute_bars=_bars(),
        now_minute=10,
    )
    actions = [
        ("FOLLOWED_TOOK", dict(followed=True, took=True)),
        ("FOLLOWED_SKIPPED", dict(followed=True, took=False)),
        ("OVERRODE_TOOK", dict(followed=False, took=True)),
        ("OVERRODE_SKIPPED", dict(followed=False, took=False)),
    ]

    for expected, flags in actions:
        row = dol.capture_decision_record(
            **base,
            precheck_snapshot=_precheck_snapshot(),
            followed=flags["followed"],
            took=flags["took"],
            pool_path=tmp_path / "pool.jsonl",
        )
        assert row["operator_action"] == expected
        assert row["pool"] == dol.POOL_NAME
        assert row["advisory_only"] is True
        assert row["execution_authority"] is False
        assert row["part_of_frozen_cohort"] is False


def test_log_then_label_flow_preserves_original_row_and_fills_later(tmp_path):
    pool_path = tmp_path / "pool.jsonl"
    row = dol.capture_decision_record(
        symbol="SPY",
        trade_type="option",
        implied_direction="bullish",
        followed=False,
        took=True,
        decided_at=datetime(2026, 8, 13, 14, 30, tzinfo=timezone.utc),
        precheck_snapshot=_precheck_snapshot(),
        minute_bars=_bars(),
        now_minute=10,
        pool_path=pool_path,
    )
    original = dict(row)
    labeled = dol.label_outcome(
        row,
        a2_episode={
            "side": "CALL",
            "session_date": "2026-08-13",
            "decided_at": "2026-08-13T14:30:00+00:00",
            "regime_tag": {"state": "TREND_UP"},
        },
        session_quotes=[
            {"provider_ts": datetime(2026, 8, 13, 14, 29, 59, tzinfo=timezone.utc), "mid": 100.0, "bid": 99.99, "ask": 100.01, "source": "test"},
            {"provider_ts": datetime(2026, 8, 13, 15, 30, 1, tzinfo=timezone.utc), "mid": 101.0, "bid": 100.99, "ask": 101.01, "source": "test"},
        ],
        pool_path=pool_path,
    )
    assert row == original
    assert row["record_type"] == "DECISION"
    assert labeled["record_type"] == "OUTCOME_LABEL"
    assert labeled["outcome_label_source"] == "a2_measurement.label_episode"
    assert labeled["signed_return_60m"] is None or isinstance(labeled["signed_return_60m"], float)


def test_capture_writes_decision_before_outcome_label(tmp_path):
    pool_path = tmp_path / "pool.jsonl"
    decision = dol.capture_decision_record(
        symbol="SPY",
        trade_type="option",
        implied_direction="bullish",
        followed=True,
        took=False,
        precheck_snapshot=_precheck_snapshot(),
        minute_bars=_bars(),
        now_minute=10,
        pool_path=pool_path,
    )

    rows_before_label = [json.loads(line) for line in pool_path.read_text().splitlines()]
    assert rows_before_label == [decision]
    assert rows_before_label[0]["record_type"] == "DECISION"

    label = dol.label_outcome(decision, pool_path=pool_path)
    rows_after_label = [json.loads(line) for line in pool_path.read_text().splitlines()]
    assert [row["record_type"] for row in rows_after_label] == ["DECISION", "OUTCOME_LABEL"]
    assert rows_after_label[1] == label
    assert datetime.fromisoformat(label["recorded_at"]) >= datetime.fromisoformat(decision["recorded_at"])


def test_duplicate_decision_id_returns_original_without_appending(tmp_path):
    pool_path = tmp_path / "pool.jsonl"
    first = dol.capture_decision_record(
        symbol="SPY", trade_type="option", implied_direction="bullish",
        followed=True, took=True, precheck_snapshot=_precheck_snapshot(),
        minute_bars=_bars(), now_minute=10, decision_id="fill-123",
        pool_path=pool_path,
    )
    duplicate = dol.capture_decision_record(
        symbol="SPY", trade_type="option", implied_direction="bullish",
        followed=False, took=True, precheck_snapshot=_precheck_snapshot(),
        minute_bars=_bars(), now_minute=11, decision_id="fill-123",
        pool_path=pool_path,
    )
    assert duplicate == first
    assert len(dol.read_pool_records(pool_path=pool_path)) == 1


def test_outcome_revision_links_to_decision_and_latest_reader_wins(tmp_path):
    pool_path = tmp_path / "pool.jsonl"
    decision = dol.capture_decision_record(
        symbol="SPY",
        trade_type="option",
        implied_direction="bearish",
        followed=False,
        took=True,
        precheck_snapshot=_precheck_snapshot(),
        minute_bars=_bars(),
        now_minute=10,
        pool_path=pool_path,
    )
    first_label = dol.label_outcome(decision, pool_path=pool_path)
    second_label = dol.label_outcome(first_label, pool_path=pool_path)

    assert first_label["decision_id"] == decision["decision_id"]
    assert first_label["supersedes_record_id"] == decision["record_id"]
    assert second_label["supersedes_record_id"] == first_label["record_id"]
    assert dol.read_latest_labeled_revisions(pool_path=pool_path) == {
        decision["decision_id"]: second_label
    }


def test_missing_inputs_stay_missing_or_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(dol.regime_layer_v0, "classify_regime", lambda **_kwargs: None)
    row = dol.capture_decision_record(
        symbol="SPY",
        trade_type="option",
        implied_direction="bullish",
        followed=True,
        took=False,
        precheck_snapshot=None,
        minute_bars=[],
        now_minute=10,
        pool_path=tmp_path / "pool.jsonl",
    )
    assert row["precheck_status"] == "MISSING"
    assert row["regime_tag"] == "UNKNOWN"
    assert row["outcome_status"] == "UNAVAILABLE"
    assert row["signed_return_60m"] is None


def test_skipped_trade_labeling_is_supported(tmp_path):
    row = dol.capture_decision_record(
        symbol="SPY",
        trade_type="option",
        implied_direction="bearish",
        followed=False,
        took=False,
        precheck_snapshot=_precheck_snapshot(),
        minute_bars=_bars(),
        now_minute=10,
        pool_path=tmp_path / "pool.jsonl",
    )
    assert row["operator_action"] == "OVERRODE_SKIPPED"


def test_underpowered_floor_is_reported():
    rows = [
        {
            "record_type": "DECISION",
            "decision_id": str(index),
            "operator_action": "OVERRODE_TOOK",
        }
        for index in range(49)
    ]
    rows.extend(
        {"record_type": "OUTCOME_LABEL", "decision_id": str(index)}
        for index in range(49)
    )
    summary = dol.summarize_pool_counts(rows)
    assert summary["status"] == "UNDERPOWERED"
    assert summary["n_rows"] == 98
    assert summary["n_decisions"] == 49
    assert summary["n_override_decisions"] == 49
    assert summary["n_outcome_labels"] == 49
    assert summary["underpowered_floor"] == 50
