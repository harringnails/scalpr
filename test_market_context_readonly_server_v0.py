import json
from pathlib import Path

import market_context_readonly_server_v0 as server


def observation(at, value):
    return {
        "record_type": "MARKET_CONTEXT_OBSERVATION",
        "observed_at_utc": at,
        "data_freshness": "CLEAN",
        "execution_authority": False,
        "fields": {"spy_structure": {"spy_mid": {"value": value}}},
    }


def test_latest_valid_observation_is_returned_without_modifying_ledger(tmp_path):
    ledger = tmp_path / "context.jsonl"
    first = observation("2026-09-03T14:00:00Z", 770.0)
    latest = observation("2026-09-03T14:01:00Z", 771.0)
    ledger.write_text("\n".join((json.dumps(first), "{partial", json.dumps(latest), "")))
    before = (ledger.read_bytes(), ledger.stat().st_mtime_ns)

    payload = server.response_payload(ledger)

    assert payload["ledger_status"] == "AVAILABLE"
    assert payload["record"] == latest
    assert payload["record_count"] == 2
    assert payload["execution_authority"] is False
    assert (ledger.read_bytes(), ledger.stat().st_mtime_ns) == before


def test_missing_and_empty_ledgers_are_safe_insufficient_states(tmp_path):
    missing = server.response_payload(tmp_path / "missing.jsonl")
    assert missing["ledger_status"] == "MISSING" and missing["record"] is None
    assert missing["record_count"] == 0

    empty_path = tmp_path / "empty.jsonl"
    empty_path.write_text("\n{bad-json\n")
    empty = server.response_payload(empty_path)
    assert empty["ledger_status"] == "EMPTY" and empty["record"] is None
    assert empty["record_count"] == 0


def test_source_has_no_ledger_write_path():
    source = Path(server.__file__).read_text()
    assert '.open("w"' not in source
    assert '.open("a"' not in source
    assert "write_text(" not in source
    assert "write_bytes(" not in source
