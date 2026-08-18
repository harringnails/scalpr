"""Network-free tests for provider-neutral Unusual Whales flow ingestion."""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from institutional_flow import aggregate_flow_events, normalize_unusual_whales_alert
from institutional_flow_store import InstitutionalFlowStore
from institutional_flow_ingestion import InstitutionalFlowIngestionService
from unusual_whales_adapter import (
    FLOW_ALERT_LIMIT, UnusualWhalesAdapter, UnusualWhalesError,
    UnusualWhalesRateLimited,
)


NOW = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)


def ms(value):
    return int(value.timestamp() * 1000)


def alert(event_id="flow-1", *, right="C", minutes_ago=1, ask_prem=120000,
          bid_prem=20000, premium=140000, sweep=True):
    return {
        "id": event_id,
        "ticker": "SPY",
        "option_chain": f"SPY260807{right}00775000",
        "underlying_price": "773.20",
        "volume": 500,
        "total_size": 100,
        "total_premium": premium,
        "total_ask_side_prem": ask_prem,
        "total_bid_side_prem": bid_prem,
        "executed_at": ms(NOW - timedelta(minutes=minutes_ago)),
        "price": "1.40",
        "has_multileg": False,
        "has_sweep": sweep,
        "open_interest": 300,
        "ask_vol": 80,
        "bid_vol": 20,
        "bid": "1.35",
        "ask": "1.40",
        "exchanges": ["CBOE"],
    }


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        return self.responses.pop(0)


def test_normalization_preserves_times_raw_payload_and_semantics():
    raw = alert()
    event = normalize_unusual_whales_alert(raw, received_at=NOW, provider_latency_ms=12.5)
    assert event.provider == "unusual_whales"
    assert event.observed_at == NOW - timedelta(minutes=1)
    assert event.received_at == NOW
    assert event.option_type == "CALL" and event.strike == Decimal("775")
    assert event.expiration.date().isoformat() == "2026-08-07"
    assert event.execution_side == "ASK" and event.sentiment == "BULLISH"
    assert event.is_sweep is True and event.source_confidence is None
    assert event.raw_payload == raw


def test_put_ask_is_bearish_and_fallback_dedupe_is_deterministic():
    raw = alert("", right="P")
    first = normalize_unusual_whales_alert(raw, received_at=NOW)
    second = normalize_unusual_whales_alert(raw, received_at=NOW)
    assert first.execution_side == "ASK" and first.sentiment == "BEARISH"
    assert first.dedupe_key == second.dedupe_key == first.event_id


def test_rest_flow_alert_shape_maps_created_at_expiry_strike_and_type():
    raw = alert("")
    raw["created_at"] = raw.pop("executed_at")
    raw["expiry"] = "2026-08-07"
    raw["strike"] = "775"
    raw["type"] = "call"
    event = normalize_unusual_whales_alert(raw, received_at=NOW)
    assert event.observed_at == NOW - timedelta(minutes=1)
    assert event.expiration.date().isoformat() == "2026-08-07"
    assert event.strike == Decimal("775") and event.option_type == "CALL"


def test_missing_event_window_is_unknown_not_neutral():
    snapshot = aggregate_flow_events([], ticker="SPY", window_minutes=5, window_end=NOW)
    assert snapshot.institutional_flow_status == "UNAVAILABLE"
    assert snapshot.institutional_flow_age_seconds is None
    assert snapshot.net_directional_premium is None
    assert snapshot.flow_direction_score is None


def test_successful_empty_provider_window_is_observed_neutral_not_missing():
    snapshot = aggregate_flow_events(
        [], ticker="SPY", window_minutes=5, window_end=NOW,
        provider_available=True)
    assert snapshot.institutional_flow_status == "AVAILABLE"
    assert snapshot.event_count == 0
    assert snapshot.net_directional_premium == 0
    assert snapshot.flow_direction_score == 0


def test_rolling_features_have_documented_ranges_and_no_recommendation():
    events = [
        normalize_unusual_whales_alert(alert("call", minutes_ago=4), received_at=NOW),
        normalize_unusual_whales_alert(
            alert("put", right="P", minutes_ago=1, ask_prem=90000,
                  bid_prem=10000, premium=100000), received_at=NOW),
    ]
    snapshot = aggregate_flow_events(events, ticker="SPY", window_minutes=5, window_end=NOW)
    assert snapshot.event_count == 2
    assert snapshot.net_call_premium == Decimal("100000")
    assert snapshot.net_put_premium == Decimal("80000")
    assert snapshot.net_directional_premium == Decimal("20000")
    assert 0 <= snapshot.bullish_flow_score <= 1
    assert 0 <= snapshot.bearish_flow_score <= 1
    assert -1 <= snapshot.flow_direction_score <= 1
    assert snapshot.opening_position_probability is None
    assert snapshot.recommendation is None and snapshot.qualifying is False


def test_adapter_deduplicates_and_filters_lookback_without_blocking_api_shape():
    session = Session([Response({"data": [alert(), alert()]})])
    adapter = UnusualWhalesAdapter(
        "test-token", session=session, max_retries=0, clock=lambda: NOW)
    events = asyncio.run(adapter.fetch_recent_events("spy", 5))
    assert len(events) == 1
    assert adapter.status()["events_deduplicated"] == 1
    assert session.calls[0][0].endswith("/stock/SPY/flow-alerts")
    assert FLOW_ALERT_LIMIT == 200
    assert session.calls[0][1] == {"limit": FLOW_ALERT_LIMIT}
    assert "test-token" not in str(adapter.status())


def test_rate_limit_and_missing_key_fail_closed_without_secret_leak():
    adapter = UnusualWhalesAdapter(
        "test-token", session=Session([Response({}, 429)]),
        max_retries=0, clock=lambda: NOW)
    try:
        asyncio.run(adapter.fetch_recent_events("SPY", 5))
    except UnusualWhalesRateLimited as exc:
        assert str(exc) == "Unusual Whales API HTTP 429"
        assert "test-token" not in str(exc)
    else:
        raise AssertionError("rate-limited response was accepted")
    missing = UnusualWhalesAdapter("", session=Session([]), clock=lambda: NOW)
    try:
        asyncio.run(missing.fetch_recent_events("SPY", 5))
    except UnusualWhalesError as exc:
        assert "UW_API_KEY" in str(exc)
    else:
        raise AssertionError("missing credential was accepted")


def test_store_has_required_tables_and_deduplicates():
    event = normalize_unusual_whales_alert(alert(), received_at=NOW)
    snapshot = aggregate_flow_events([event], ticker="SPY", window_minutes=5, window_end=NOW)
    with TemporaryDirectory() as directory:
        store = InstitutionalFlowStore(Path(directory) / "flow.db")
        assert store.append_event(event) is True
        assert store.append_event(event) is False
        assert store.append_snapshot(snapshot) is True
        store.record_health({"provider": "unusual_whales", "status": "AVAILABLE"})
        store.record_error("unusual_whales", "malformed_event", "missing ticker")
        assert store.status() == {
            "available": True, "path": str(Path(directory) / "flow.db"),
            "events": 1, "snapshots": 1, "health_records": 1,
            "ingestion_errors": 1,
        }


def test_store_returns_latest_snapshot_for_requested_window():
    with TemporaryDirectory() as directory:
        store = InstitutionalFlowStore(Path(directory) / "flow.db")
        event = normalize_unusual_whales_alert(alert(), received_at=NOW)
        first = aggregate_flow_events([event], ticker="SPY", window_minutes=5, window_end=NOW)
        latest = aggregate_flow_events([event], ticker="SPY", window_minutes=5,
                                       window_end=NOW + timedelta(minutes=1))
        other = aggregate_flow_events([event], ticker="SPY", window_minutes=1,
                                      window_end=NOW + timedelta(minutes=1))
        assert store.append_snapshot(first) is True
        assert store.append_snapshot(other) is True
        assert store.append_snapshot(latest) is True
        assert store.latest_snapshot(ticker="SPY", window_minutes=5).window_end == latest.window_end
        assert store.latest_snapshot(ticker="SPY", window_minutes=1).window_end == other.window_end


def test_one_shot_ingestion_persists_events_snapshots_and_health():
    session = Session([Response({"data": [alert()]})])
    with TemporaryDirectory() as directory:
        store = InstitutionalFlowStore(Path(directory) / "flow.db")
        service = InstitutionalFlowIngestionService(
            UnusualWhalesAdapter(
                "test-token", session=session, max_retries=0,
                clock=lambda: NOW),
            store, windows=(1, 5), clock=lambda: NOW)
        result = asyncio.run(service.run_once(ticker="SPY", lookback_minutes=5))
        assert result == {
            "provider_status": "AVAILABLE", "events_received": 1,
            "events_persisted": 1, "snapshots": 2,
            "execution_authority": False,
        }
        status = store.status()
        assert status["events"] == 1 and status["snapshots"] == 2
        assert status["health_records"] == 1 and status["ingestion_errors"] == 0


def test_ingestion_snapshots_retain_deduplicated_events_for_full_window():
    current = {"now": NOW}
    session = Session([
        Response({"data": [alert()]}),
        Response({"data": [alert()]}),
    ])
    with TemporaryDirectory() as directory:
        path = Path(directory) / "flow.db"
        store = InstitutionalFlowStore(path)
        clock = lambda: current["now"]
        service = InstitutionalFlowIngestionService(
            UnusualWhalesAdapter(
                "test-token", session=session, max_retries=0, clock=clock),
            store, windows=(5,), clock=clock)
        first = asyncio.run(service.run_once(ticker="SPY", lookback_minutes=5))
        assert first["events_received"] == 1 and first["events_persisted"] == 1

        current["now"] = NOW + timedelta(minutes=1)
        second = asyncio.run(service.run_once(ticker="SPY", lookback_minutes=5))
        assert second["events_received"] == 0 and second["events_persisted"] == 0
        assert store.events_between(
            ticker="SPY", start=NOW - timedelta(minutes=5),
            end=current["now"])

        with sqlite3.connect(path) as conn:
            raw = conn.execute("""
                SELECT raw_json FROM institutional_flow_snapshots
                ORDER BY window_end DESC LIMIT 1
            """).fetchone()[0]
        snapshot = json.loads(raw)
        assert snapshot["event_count"] == 1


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_"):
            test()
            print("PASS", name)
