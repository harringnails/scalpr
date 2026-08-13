"""Network-free safety tests for Guard quotes, reconciliation, scope, and rotation."""

from __future__ import annotations

import csv
import gzip
import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import scalp_server as ss
import scope_policy
from health_events import HealthEventLog
from storage_maintenance import rotate_csv, sha256_file, storage_health


def _guard(symbol="SPY260803C00600000"):
    return ss.Guard({
        "symbol": symbol,
        "type": "option",
        "ladder": [{"at": 0, "tol": 15}, {"at": 15, "tol": 6}],
        "grace_seconds": 0,
        "confirm_ticks": 2,
    }, entry=1.0, qty=1)


def test_missing_bid_never_changes_price_peak_or_breach():
    guard = _guard()
    guard.on_price(1.20)
    before = (guard.last, guard.peak, guard.breach, guard.last_update)
    guard.mark_quote_unavailable("NO_BID", "ask exists but is ignored")
    after = (guard.last, guard.peak, guard.breach, guard.last_update)
    assert after == before
    snap = guard.snapshot()
    assert snap["quote_status"] == "NO_BID"
    assert snap["last"] is None and snap["profit"] is None
    assert snap["execution_protected"] is False


def test_resume_fails_closed_without_bid():
    guard = _guard()
    guard.paused = True
    try:
        guard.resume()
    except ValueError as exc:
        assert "executable bid" in str(exc)
    else:
        raise AssertionError("resume should fail without a current executable bid")


class _Position:
    def __init__(self, symbol):
        self.symbol = symbol


class _Trading:
    def __init__(self, symbols=None, error=None):
        self.symbols = symbols or []
        self.error = error

    def get_all_positions(self):
        if self.error:
            raise self.error
        return [_Position(symbol) for symbol in self.symbols]


def _platform_for_reconcile(guard, trading, health_path):
    platform = object.__new__(ss.Platform)
    platform.guards = {guard.symbol: guard}
    platform.trading = trading
    platform._last_reconcile = 0.0
    platform.health = HealthEventLog(health_path, heartbeat_seconds=999)
    return platform


def test_reconciliation_retires_only_confirmed_orphans():
    with tempfile.TemporaryDirectory() as td:
        guard = _guard()
        platform = _platform_for_reconcile(guard, _Trading([]), Path(td) / "health.jsonl")
        assert platform._maybe_reconcile_guards(0) == [guard.symbol]
        assert guard.done is True

        guard2 = _guard("SPY260803P00600000")
        platform2 = _platform_for_reconcile(
            guard2, _Trading(error=RuntimeError("broker unavailable")), Path(td) / "health2.jsonl")
        assert platform2._maybe_reconcile_guards(0) == []
        assert guard2.done is False


def test_spy_zero_to_two_dte_scope():
    as_of = date(2026, 8, 3)
    assert scope_policy.validate_option("SPY260803C00600000", as_of)["dte"] == 0
    assert scope_policy.validate_option("SPY260805P00600000", as_of)["dte"] == 2
    for symbol in ("QQQ260803C00600000", "SPY260806C00600000"):
        try:
            scope_policy.validate_option(symbol, as_of)
        except scope_policy.ScopeError:
            pass
        else:
            raise AssertionError(f"out-of-scope symbol accepted: {symbol}")
    try:
        scope_policy.validate_trade("SPY", "stock", as_of)
    except scope_policy.ScopeError:
        pass
    else:
        raise AssertionError("SPY stock should be outside the options-only V2 scope")


def test_csv_rotation_is_lossless_and_manifested():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "ticks.csv"
        with source.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["utc_time", "symbol", "bid"])
            writer.writerow(["t1", "SPY", "600.00"])
        original = source.read_bytes()
        rec = rotate_csv(source, root / "archives", max_bytes=1,
                         now=datetime(2026, 8, 3, tzinfo=timezone.utc))
        archive = root / "archives" / rec["archive"]
        with gzip.open(archive, "rb") as handle:
            assert handle.read() == original
        assert source.read_text() == "utc_time,symbol,bid\n"
        assert rec["archive_sha256"] == sha256_file(archive)
        manifest = [json.loads(line) for line in (root / "archives" / "rotation_manifest_v1.jsonl").read_text().splitlines()]
        assert manifest == [rec]
        health = storage_health(root, tracked=["ticks.csv", "archives"])
        assert health["tracked_bytes"]["ticks.csv"] > 0
        assert health["tracked_bytes"]["archives"] >= archive.stat().st_size


def test_health_log_failure_never_raises():
    with tempfile.TemporaryDirectory() as td:
        directory = Path(td) / "not-a-file"
        directory.mkdir()
        assert HealthEventLog(directory).record("guard", "OK") is None


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
