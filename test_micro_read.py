"""Deterministic, network-free tests for the rolling Micro Read input path."""

import csv
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import micro_read as mr


def check(name, condition):
    print(f"  {'ok' if condition else 'FAIL'}: {name}")
    assert condition, name


def _rows(symbol, count=30, start_price=100.0):
    now = datetime.now(timezone.utc)
    return [{
        "utc_time": (now - timedelta(seconds=count - i)).isoformat(),
        "provider_ts": "",
        "symbol": symbol,
        "bid": "",
        "ask": "",
        "bid_size": 10 + i,
        "ask_size": 8,
        "mid": start_price + i * 0.01,
        "spread": "",
    } for i in range(count)]


def test_live_buffer_works_without_csv():
    symbol = "BUFFERTEST"
    original = mr.TICK_LOG
    mr._clear_buffer(symbol)
    try:
        mr.TICK_LOG = Path("definitely-missing-tick-log.csv")
        mr.record_ticks(_rows(symbol))
        result = mr.compute_read(symbol)
        check("buffer supplies enough ticks", result["available"] and result["n"] == 30)
        check("linear series reads upward", result["lean"] == "up")
    finally:
        mr.TICK_LOG = original
        mr._clear_buffer(symbol)


def test_bounded_tail_bootstraps_after_restart():
    symbol = "TAILTEST"
    original_log, original_tail = mr.TICK_LOG, mr.TAIL_BYTES
    mr._clear_buffer(symbol)
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ticks.csv"
            rows = _rows(symbol, count=40)
            with path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=mr.TICK_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            mr.TICK_LOG = path
            mr.TAIL_BYTES = 4096
            result = mr.compute_read(symbol)
            check("tail bootstrap supplies recent ticks", result["available"])
            check("tail bootstrap does not require live buffer", result["n"] >= mr.MIN_TICKS)
    finally:
        mr.TICK_LOG, mr.TAIL_BYTES = original_log, original_tail
        mr._clear_buffer(symbol)


if __name__ == "__main__":
    test_live_buffer_works_without_csv()
    test_bounded_tail_bootstraps_after_restart()
    print("\nALL MICRO READ TESTS PASSED")
