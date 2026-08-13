"""Network-free tests for timestamp-audited tick capture."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from scalp_server import _tick_quote_row


def check(name, condition):
    print(f"  {'ok' if condition else 'FAIL'}: {name}")
    assert condition, name


def quote(timestamp, bid=100.0, ask=100.02):
    return SimpleNamespace(
        timestamp=timestamp, bid_price=bid, ask_price=ask,
        bid_size=10, ask_size=12)


def test_fresh_provider_event_is_captured_with_both_times():
    received = datetime(2026, 8, 5, 15, 0, 10, tzinfo=timezone.utc)
    event = received - timedelta(seconds=2)
    row, reason, age = _tick_quote_row(
        "spy", quote(event), received_at=received, stale_seconds=30)
    check("fresh event accepted", row is not None and reason is None)
    check("provider and receipt timestamps stay distinct",
          row["provider_ts"] == event.isoformat() and row["utc_time"] == received.isoformat())
    check("event age retained for diagnostics", age == 2.0)


def test_stale_or_unaudited_events_are_rejected():
    received = datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc)
    row, reason, age = _tick_quote_row(
        "SPY", quote(received - timedelta(minutes=5)),
        received_at=received, stale_seconds=30)
    check("stale latest quote is not repeated", row is None and reason == "STALE_PROVIDER_TIMESTAMP")
    check("stale age is explicit", age == 300.0)
    row, reason, age = _tick_quote_row(
        "SPY", quote(None), received_at=received, stale_seconds=30)
    check("missing provider timestamp fails closed",
          row is None and reason == "MISSING_PROVIDER_TIMESTAMP" and age is None)


if __name__ == "__main__":
    test_fresh_provider_event_is_captured_with_both_times()
    test_stale_or_unaudited_events_are_rejected()
    print("\nALL TICK CAPTURE TESTS PASSED")
