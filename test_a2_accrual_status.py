from datetime import date, datetime, timedelta, timezone
import json

import pytest

import a2_accrual_status as status
import a2_accrual_store as store


BASE = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)


def _label(index, *, minutes=0, side="CALL", key=None, available=True):
    decided = BASE + timedelta(minutes=minutes)
    return {
        "episode_key": key or f"episode-{index}",
        "episode_record_id": f"record-{index}",
        "symbol": "SPY",
        "side": side,
        "decided_at": decided.isoformat(),
        "session_date": decided.date().isoformat(),
        "endpoint_source": store.DENSE_ENDPOINT_SOURCE,
        "label_status": "AVAILABLE" if available else "UNAVAILABLE",
    }


def _summary(count):
    return {
        "endpoint_source": store.DENSE_ENDPOINT_SOURCE,
        "data_integrity_status": "PASS",
        "clean_a2_labelable_episode_count": count,
    }


def test_non_overlap_uses_first_episode_key_and_same_side_60m_scope():
    labels = [
        _label(1, minutes=0, key="same"),
        _label(2, minutes=1, key="same"),
        _label(3, minutes=30),
        _label(4, minutes=30, side="PUT"),
        _label(5, minutes=60),
        _label(6, minutes=65, available=False),
    ]

    result = status.count_non_overlapping(labels)

    assert result["raw_labelable_count"] == 5
    assert result["non_overlapping_count"] == 3
    assert result["gap_count"] == 2
    assert result["exclusion_reason_counts"] == {
        "duplicate_episode_key": 1,
        "overlapping_60m_horizon_same_symbol_side": 1,
    }


def test_rate_math_uses_all_elapsed_xnys_sessions():
    report = status.build_status(
        summary=_summary(9),
        labels=[_label(i, minutes=60 * i) for i in range(9)],
        summary_path=status.Path("summary.json"),
        labels_path=status.Path("labels.jsonl"),
        as_of_session=date(2026, 8, 28),
        generated_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )

    assert report["accrual_window"]["elapsed_trading_sessions"] == 11
    assert report["fire_rate"]["non_overlapping_episodes_per_trading_session"] == 0.818182
    assert report["fire_rate"]["non_overlapping_episodes_per_five_session_week"] == 4.090909


def test_projection_is_wide_and_unstable_below_sample_floor():
    projection = status.projection(
        count=9, elapsed_sessions=11, as_of_session=date(2026, 8, 28))

    assert projection["status"] == "UNSTABLE_INSUFFICIENT_SAMPLE"
    assert projection["insufficient_sample_reasons"] == [
        "episodes_below_30", "elapsed_sessions_below_15"]
    assert "constant_rate_point_estimate" not in projection
    sessions = projection["additional_trading_sessions_range"]
    assert sessions["fast"] < sessions["slow"]
    dates = projection["projected_calendar_date_range"]
    assert date.fromisoformat(dates["earliest"]) < date.fromisoformat(dates["latest"])
    assert projection["planning_only"] is True
    assert projection["changes_verdict_threshold"] is False


def test_xnys_calendar_excludes_weekends_and_market_holidays():
    sessions = status.trading_sessions(date(2026, 12, 24), date(2026, 12, 28))

    assert sessions == [date(2026, 12, 24), date(2026, 12, 28)]
    assert status.is_xnys_session(date(2027, 3, 26)) is False  # Good Friday


def test_summary_count_mismatch_fails_closed():
    with pytest.raises(status.AccrualStatusError, match="label_count_mismatch"):
        status.build_status(
            summary=_summary(2), labels=[_label(1)],
            summary_path=status.Path("summary.json"),
            labels_path=status.Path("labels.jsonl"),
            as_of_session=date(2026, 8, 14))


def test_cli_fails_closed_without_dense_store_and_does_not_write(tmp_path, capsys):
    output = tmp_path / "status.json"

    exit_code = status.main([
        "--summary", str(tmp_path / "missing-summary.json"),
        "--labels", str(tmp_path / "missing-labels.jsonl"),
        "--output", str(output),
        "--as-of-session", "2026-08-28",
    ])

    assert exit_code == 2
    assert "dense_a2_summary_missing" in capsys.readouterr().err
    assert not output.exists()


def test_cli_writes_derived_status_and_prints_hedged_summary(tmp_path, capsys):
    labels = tmp_path / "labels.jsonl"
    summary = tmp_path / "summary.json"
    output = tmp_path / "status.json"
    rows = [_label(i, minutes=60 * i) for i in range(9)]
    labels.write_text("".join(json.dumps(row) + "\n" for row in rows))
    summary.write_text(json.dumps({
        **_summary(9),
        "clean_a2_unavailable_episode_count": 0,
    }))

    exit_code = status.main([
        "--summary", str(summary), "--labels", str(labels),
        "--output", str(output), "--as-of-session", "2026-08-28",
    ])

    printed = capsys.readouterr().out
    report = json.loads(output.read_text())
    assert exit_code == 0
    assert "A2 accrual: 9 / 200" in printed
    assert "UNSTABLE_INSUFFICIENT_SAMPLE" in printed
    assert report["gate"]["non_overlapping_count"] == 9
    assert "constant_rate_point_estimate" not in report["projection"]
