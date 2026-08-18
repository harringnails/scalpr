from datetime import datetime, timedelta, timezone

import regime_flow_runner_measurement_v0 as measurement


NOW = datetime(2026, 8, 14, 16, 0, tzinfo=timezone.utc)
LADDER = [{"at": 0, "tol": 15}, {"at": 15, "tol": 6}]


def _recorder(tmp_path):
    return measurement.RunnerMeasurement(
        symbol="SPY260814C00777000", kind="option", entry_price=1.0,
        quantity=1, opened_at=NOW, ladder=LADDER, grace_seconds=0,
        confirm_ticks=2, stall_seconds=0, stall_min_profit=20,
        runner_policy={"version": "regime-flow-runner-v1", "enabled": True},
        position_direction="CALL", path=tmp_path / "runner-shadow.jsonl",
    )


def _active_snapshot():
    return {
        "status": "ACTIVE",
        "reason_codes": ["REGIME_AND_FLOW_ALIGNED"],
        "baseline_tolerance_pct": 6,
        "effective_tolerance_pct": 10,
    }


def _observation():
    return {
        "regime": {
            "state": "trending_up", "status": "validation_ready",
            "metadata": {"fit_end_time": NOW.isoformat()},
        },
        "flow": {"tier": "GREEN", "direction": "BULLISH", "as_of": NOW.isoformat()},
    }


def _record_path(recorder):
    for seconds, price in ((0, 1.00), (1, 1.20), (2, 1.12), (3, 1.12), (4, 1.09)):
        recorder.record_mark(price=price, observed_at=NOW + timedelta(seconds=seconds),
                             runner_snapshot=_active_snapshot())


def test_static_replay_matches_hand_computed_exit_and_runner_exits_later(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.record_runner_transition(
        before_status="AWAITING_CONFIRMATION", after_snapshot=_active_snapshot(),
        observation=_observation(), observed_at=NOW,
    )
    _record_path(recorder)

    result = recorder.finalize(
        runner_exit_price=1.09, exit_reason="runner dip",
        exited_at=NOW + timedelta(seconds=4),
    )

    assert result["paired_outcome_status"] == "AVAILABLE"
    assert result["paired_outcome_tag"] == "PAIRED"
    assert result["runner_activated"] is True
    assert result["static_exit_price"] == 1.12
    assert result["static_exit_pct"] == 12.0
    assert result["runner_exit_pct"] == 9.0
    assert result["paired_delta_runner_minus_static_pct"] == -3.0
    assert result["runner_exit_timestamp"] >= result["static_exit_timestamp"]
    assert result["record_appended"] is True


def test_no_activation_is_logged_with_zero_delta(tmp_path):
    recorder = _recorder(tmp_path)
    _record_path(recorder)

    result = recorder.finalize(
        runner_exit_price=1.12, exit_reason="static dip",
        exited_at=NOW + timedelta(seconds=3),
    )

    assert result["paired_outcome_status"] == "AVAILABLE"
    assert result["paired_outcome_tag"] == "NO_ACTIVATION"
    assert result["runner_activated"] is False
    assert result["paired_delta_runner_minus_static_pct"] == 0.0
    assert result["static_exit_pct"] == result["runner_exit_pct"]


def test_missing_marks_are_unavailable_and_retained(tmp_path):
    result = _recorder(tmp_path).finalize(
        runner_exit_price=1.0, exit_reason="manual exit", exited_at=NOW)

    assert result["paired_outcome_status"] == "UNAVAILABLE"
    assert result["paired_outcome_tag"] == "UNAVAILABLE"
    assert result["unavailable_reason"] == "missing_confirmed_marks"
    assert result["record_appended"] is True


def test_analysis_is_underpowered_below_pre_registered_activation_floor():
    records = [
        {
            "record_type": "REGIME_FLOW_RUNNER_PAIRED_OUTCOME",
            "paired_outcome_status": "AVAILABLE",
            "runner_activated": True,
            "paired_delta_runner_minus_static_pct": 0.25,
            "runner_exit_timestamp": (NOW + timedelta(minutes=index)).isoformat(),
        }
        for index in range(29)
    ]

    report = measurement.analyze_records(records, permutations=100)

    assert report["activated_available_positions"] == 29
    assert report["verdict"] == "UNDERPOWERED"
    assert report["activation_floor"] == 30
    assert report["null_p_value_formula"] == "(k+1)/(n+1)"
