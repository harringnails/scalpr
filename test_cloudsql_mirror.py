"""Network-free tests for the incremental Cloud SQL evidence mirror."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from cloudsql_mirror import (_collect_missing, _feature_row, _raw_row,
                             _read_jsonl_batch)


def test_raw_row_is_deterministic_and_preserves_event_time():
    record = {"symbol": "SPY", "ts": "2026-08-05T20:00:00+00:00", "value": 1}
    first = _raw_row(record, "scalpr", "test", "v0")
    second = _raw_row(record, "scalpr", "test", "v0")
    assert first["content_hash"] == second["content_hash"]
    assert first["provider_timestamp"] == record["ts"]
    assert json.loads(first["payload"]) == record


def test_feature_row_keeps_missing_inputs_explicit():
    record = {
        "ticker": "SPY", "schema_version": "scalpr-intel-v0",
        "decision_timestamp": "2026-08-05T20:00:00+00:00",
        "feature_snapshot_hash": "a" * 64,
        "feature_record": {
            "ticker": "SPY", "_unwired": ["breadth"],
            "options": {"missing_inputs": ["iv_rank"]},
        },
    }
    row = _feature_row(record)
    assert row["content_hash"] == "a" * 64
    assert json.loads(row["missing_inputs"]) == ["breadth", "options.iv_rank"]
    assert _collect_missing(record["feature_record"]) == ["breadth", "options.iv_rank"]


def test_jsonl_reader_advances_only_over_complete_valid_records():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "stream.jsonl"
        path.write_bytes(b'{"n":1}\n{"n":2}\n{"n":3}')
        first, offset = _read_jsonl_batch(path, 0, 1)
        second, offset2 = _read_jsonl_batch(path, offset, 10)
        assert first == [{"n": 1}] and second == [{"n": 2}]
        assert offset2 < path.stat().st_size


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_"):
            test()
            print("PASS", name)
