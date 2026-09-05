import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import market_structure_readonly_server_v0 as chart


NOW = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)


class Bars:
    def __init__(self, rows):
        self.rows = rows

    def fetch(self, symbol, timeframe, now):
        return self.rows


def candle(stamp, price=100.0):
    return {"ts": stamp.isoformat(), "open": price, "high": price + 0.2,
            "low": price - 0.2, "close": price + 0.1, "volume": 1000.0}


def write_row(path, row):
    path.write_text(json.dumps(row) + "\n")


def structure(stamp, *, status="FRESH"):
    return {"record_type": "MULTI_INSTRUMENT_STRUCTURE", "symbol": "SPY",
            "observed_at_utc": stamp.isoformat(), "effective_ts_utc": stamp.isoformat(),
            "values": {"spot": 100.1, "gamma_flip": 100.0, "call_wall": 102.0,
                       "put_wall": 98.0, "gamma_regime": "positive"},
            "freshness": {"status": status, "age_seconds": 1}, "record_hash": "structure-fixture"}


def source(tmp_path, bars):
    return chart.MarketStructureSource(Bars(bars), structure_ledger=tmp_path / "structure.jsonl",
                                       episode_ledgers=[tmp_path / "episodes.jsonl"])


def test_live_payload_reads_bars_structure_and_only_accrued_markers(tmp_path):
    bridge = source(tmp_path, [candle(NOW - timedelta(minutes=1))])
    write_row(tmp_path / "structure.jsonl", structure(NOW - timedelta(seconds=30)))
    rows = [
        {"symbol": "SPY", "anchor_t0_utc": (NOW - timedelta(minutes=1)).isoformat(),
         "counts_toward_n": True, "arm": "D1", "record_hash": "counted"},
        {"symbol": "SPY", "anchor_t0_utc": NOW.isoformat(),
         "counts_toward_n": False, "arm": "D2", "record_hash": "excluded"},
    ]
    (tmp_path / "episodes.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")

    payload = bridge.fetch("SPY", "1m", NOW)

    assert payload["status"] == "LIVE"
    assert payload["candles"][0]["volume"] == 1000.0
    assert payload["structure"]["gamma_flip"] == 100.0
    assert [marker["record_hash"] for marker in payload["markers"]] == ["counted"]
    assert payload["is_inferential"] is False
    assert payload["execution_authority"] is False


def test_empty_episode_ledger_renders_zero_markers_without_fabrication(tmp_path):
    bridge = source(tmp_path, [candle(NOW - timedelta(minutes=1))])
    write_row(tmp_path / "structure.jsonl", structure(NOW - timedelta(seconds=30)))

    payload = bridge.fetch("SPY", "5m", NOW)

    assert payload["status"] == "LIVE"
    assert payload["markers"] == []


def test_old_bar_or_structure_forces_stale_state(tmp_path):
    bridge = source(tmp_path, [candle(NOW - timedelta(minutes=10))])
    write_row(tmp_path / "structure.jsonl", structure(NOW - timedelta(minutes=10)))

    payload = bridge.fetch("SPY", "1m", NOW)

    assert payload["status"] == "STALE"
    assert payload["freshness"]["bars"] == "STALE_OR_MISSING"
    assert payload["freshness"]["structure"] == "STALE_OR_MISSING"


def test_no_bars_and_no_structure_is_calm_not_started(tmp_path):
    payload = source(tmp_path, []).fetch("SPY", "1m", NOW)

    assert payload["status"] == "NOT_STARTED"
    assert payload["reason"] == "CAPTURE_NOT_STARTED"
    assert payload["candles"] == [] and payload["markers"] == []


def test_bridge_source_is_read_only_and_has_no_trade_path():
    source_text = Path(chart.__file__).read_text()
    for forbidden in ('.open("w"', '.open("a"', "write_text(", "write_bytes(",
                      "/api/order", "submitTrade", "scalp_server"):
        assert forbidden not in source_text
    assert "do_POST" in source_text and "METHOD_NOT_ALLOWED" in source_text

