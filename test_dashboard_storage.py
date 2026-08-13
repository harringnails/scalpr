"""Network-free tests for dashboard caching and the SQLite operational mirror."""

import csv
import tempfile
import threading
import time
from pathlib import Path

from dashboard_cache import DashboardCache
from operational_store import OperationalStore


def check(name, condition):
    print(f"  {'ok' if condition else 'FAIL'}: {name}")
    assert condition, name


def test_cache_reuses_value_until_ttl():
    now = [100.0]
    cache = DashboardCache(clock=lambda: now[0])
    calls = []

    def compute():
        calls.append(1)
        return {"generation": len(calls)}

    first = cache.get_or_compute("stats", 10, compute)
    now[0] += 9
    second = cache.get_or_compute("stats", 10, compute)
    now[0] += 2
    third = cache.get_or_compute("stats", 10, compute)
    check("fresh entry reused", first is second and len(calls) == 2)
    check("expired entry recomputed", third["generation"] == 2)


def test_cache_returns_stale_value_on_refresh_error():
    now = [0.0]
    cache = DashboardCache(clock=lambda: now[0])
    cache.get_or_compute("holdings", 1, lambda: {"holdings": ["safe"]})
    now[0] = 2.0

    def fail():
        raise RuntimeError("provider unavailable")

    result = cache.get_or_compute("holdings", 1, fail)
    check("stale provider snapshot retained", result == {"holdings": ["safe"]})


def test_cache_deduplicates_concurrent_tabs():
    cache = DashboardCache()
    calls, results = [], []

    def compute():
        calls.append(1)
        time.sleep(0.02)
        return {"value": 42}

    threads = [threading.Thread(
        target=lambda: results.append(cache.get_or_compute("shared", 10, compute)))
        for _ in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    check("concurrent tabs trigger one computation", len(calls) == 1)
    check("all tabs receive the shared result", results == [{"value": 42}] * 8)


def test_store_journal_sync_is_idempotent_and_ordered():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        journal = root / "journal.csv"
        fields = ["utc_time", "mode", "symbol", "realized_pct", "qty"]
        rows = [
            {"utc_time": "2026-08-01T14:00:00+00:00", "mode": "paper",
             "symbol": "SPY1", "realized_pct": "1.5", "qty": "1"},
            {"utc_time": "2026-08-01T14:01:00+00:00", "mode": "paper",
             "symbol": "SPY2", "realized_pct": "-2.0", "qty": "2"},
        ]
        with journal.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
        store = OperationalStore(root / "state.db")
        check("first sync inserts all rows", store.sync_journal(journal) == 2)
        check("second sync inserts nothing", store.sync_journal(journal) == 0)
        check("journal order preserved", [r["symbol"] for r in store.journal_rows()] == ["SPY1", "SPY2"])
        status = store.status()
        check("WAL mode active", status["journal_mode"].lower() == "wal" and status["trades"] == 2)


def test_store_mirrors_guard_events_once():
    with tempfile.TemporaryDirectory() as td:
        store = OperationalStore(Path(td) / "state.db")
        event = {"ts": "2026-08-01T14:00:00+00:00", "action": "pause", "symbol": "SPY1"}
        check("guard event inserted", store.record_guard_event(event) == 1)
        check("duplicate guard event ignored", store.record_guard_event(event) == 0)
        check("guard event counted", store.status()["guard_events"] == 1)


if __name__ == "__main__":
    test_cache_reuses_value_until_ttl()
    test_cache_returns_stale_value_on_refresh_error()
    test_cache_deduplicates_concurrent_tabs()
    test_store_journal_sync_is_idempotent_and_ordered()
    test_store_mirrors_guard_events_once()
    print("\nALL DASHBOARD STORAGE TESTS PASSED")
