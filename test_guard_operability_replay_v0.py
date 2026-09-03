import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import guard_operability_replay_v0 as replay


BASE = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)


def quote(seconds, bid, sequence=0):
    return {
        "observation_sequence": sequence,
        "option_ask": bid + 0.01,
        "option_bid": bid,
        "quote_quality": "ok",
        "ts": (BASE + timedelta(seconds=seconds)).isoformat(),
    }


def test_ratchet_fidelity_and_no_lookahead():
    rows = [quote(61, 1.30, 1), quote(62, 1.275, 2), quote(63, 1.274, 3)]
    result = replay.replay_guard(rows, entry_time=BASE.isoformat(), entry_price=1.0, quantity=1)
    assert result["status"] == "AVAILABLE"
    assert result["exit_observation_sequence"] == 3
    assert "tol 2.5%" in result["exit_reason"]
    changed_future = rows + [quote(64, 99.0, 4)]
    second = replay.replay_guard(changed_future, entry_time=BASE.isoformat(), entry_price=1.0, quantity=1)
    assert second["exit_time_utc"] == result["exit_time_utc"]
    assert second["decision_prefix_hash"] == result["decision_prefix_hash"]


def test_stall_fidelity_at_46_seconds_after_peak():
    policy = replay.GuardPolicy(grace_seconds=0)
    rows = [quote(1, 1.21, 1), quote(47, 1.21, 2)]
    result = replay.replay_guard(
        rows, entry_time=BASE.isoformat(), entry_price=1.0, quantity=1, policy=policy,
    )
    assert result["exit_observation_sequence"] == 2
    assert result["exit_reason"].startswith("stall: 45s")


def test_early_exit_is_outcome_metric_not_signal_verdict():
    rows = [
        quote(61, 1.10, 1), quote(62, 0.95, 2), quote(63, 0.94, 3),
        quote(120, 0.651, 4), quote(180, 3.0, 5),
    ]
    result = replay.replay_guard(rows, entry_time=BASE.isoformat(), entry_price=1.0, quantity=1)
    assert result["early_exit"] is True
    assert result["mae_pct"] == -34.9
    assert result["mfe_pct"] == 200.0
    assert "verdict" not in json.dumps(result).lower()


def test_run_session_is_byte_stable(tmp_path):
    journal = tmp_path / "journal.csv"
    with journal.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["utc_time", "symbol", "qty", "entry", "exit", "realized_pct"])
        writer.writeheader()
        writer.writerow({
            "utc_time": "2026-09-03T15:00:00+00:00", "symbol": replay.DEFAULT_TRADES[0][0],
            "qty": 1, "entry": 1.52, "exit": 1.67, "realized_pct": 10,
        })
    paths = tmp_path / "paths"
    snapshots = tmp_path / "snapshots"
    paths.mkdir()
    snapshots.mkdir()
    stem = f"{replay.DEFAULT_TRADES[0][0]}2026-09-03T1400000000000000"
    rows = [quote(61, 1.70, 1), quote(62, 1.45, 2), quote(63, 1.44, 3)]
    (paths / f"{stem}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    (snapshots / f"{stem}.json").write_text(json.dumps({"entry_timestamp": BASE.isoformat()}))
    output = tmp_path / "result.jsonl"
    args = dict(
        session_date="2026-09-03", journal_path=journal, paths_dir=paths,
        snapshots_dir=snapshots, output_path=output,
    )
    replay.run_session(**args)
    first = output.read_bytes()
    replay.run_session(**args)
    assert output.read_bytes() == first
