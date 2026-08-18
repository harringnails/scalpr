"""Network-free integration tests for discretionary decision capture."""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

import discretionary_override_log_v0 as override_log
import scalp_server as ss
import scope_policy
from test_manual_scope_expansion import config, occ, platform as manual_platform


def _snapshot(symbol, decision):
    parsed = scope_policy.parse_occ(symbol)
    direction = "bullish" if parsed["right"] == "C" else "bearish"
    return {
        "symbol": parsed["underlying"],
        "direction": direction,
        "weighted_read": {
            "version": "precheck-weighted-evidence-v0",
            "decision": decision,
            "agreement_pct": 70.0 if decision != "NO READ" else None,
            "input_coverage_pct": 100.0,
            "horizons": {},
        },
    }


def _capture_platform(symbol, tmp_path):
    instance = manual_platform(symbol)
    del instance._capture_manual_discretionary_decision
    instance.discretionary_override_pool_path = tmp_path / "pool.jsonl"
    instance._start_entry_enrichment = lambda _guard: None
    return instance


def test_decision_time_bars_exclude_future_provider_events(tmp_path):
    tick_path = tmp_path / "ticks.csv"
    fields = ["utc_time", "provider_ts", "symbol", "bid", "ask", "mid"]
    with tick_path.open("w", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows([
            {"utc_time": "2026-08-13T13:30:06+00:00",
             "provider_ts": "2026-08-13T13:30:05+00:00", "symbol": "SPY",
             "bid": 99.9, "ask": 100.1, "mid": 100.0},
            {"utc_time": "2026-08-13T13:30:51+00:00",
             "provider_ts": "2026-08-13T13:30:50+00:00", "symbol": "SPY",
             "bid": 100.1, "ask": 100.3, "mid": 100.2},
            {"utc_time": "2026-08-13T13:32:01+00:00",
             "provider_ts": "2026-08-13T13:32:00+00:00", "symbol": "SPY",
             "bid": 199.9, "ask": 200.1, "mid": 200.0},
        ])
    bars, now_minute = ss._decision_time_spy_bars(
        datetime(2026, 8, 13, 13, 31, tzinfo=timezone.utc), tick_path=tick_path)
    assert now_minute == 1
    assert bars == [{
        "t": 0, "open": 100.0, "high": 100.2, "low": 100.0,
        "close": 100.2, "volume": 0,
    }]


def test_manual_fill_captures_once_by_fill_id_after_guard(tmp_path, monkeypatch):
    symbol = occ("AMD", 30, right="C")
    instance = _capture_platform(symbol, tmp_path)
    monkeypatch.setattr(ss, "_decision_time_spy_bars", lambda _at: ([], 10))
    original_capture = override_log.capture_decision_record
    capture_calls = []

    def capture_after_guard(**kwargs):
        assert symbol in instance.guards
        assert instance.guards[symbol].entry == 1.0
        capture_calls.append(kwargs)
        return original_capture(**kwargs)

    monkeypatch.setattr(override_log, "capture_decision_record", capture_after_guard)
    cfg = config(symbol)
    cfg["entry_signals"] = _snapshot(symbol, "YES")
    result = instance.open_trade(cfg, manual=True)

    assert result["symbol"] == symbol
    assert len(instance.trading.submits) == 1
    assert len(capture_calls) == 1
    rows = override_log.read_pool_records(pool_path=instance.discretionary_override_pool_path)
    assert len(rows) == 1
    assert rows[0]["record_type"] == "DECISION"
    assert rows[0]["decision_id"] == "manual-fill:order-1"
    assert rows[0]["operator_action"] == "FOLLOWED_TOOK"
    assert rows[0]["capture_origin"] == "MANUAL_FILL_AUTO"
    assert rows[0]["operator_driven"] is False
    assert rows[0]["analysis_role"] == "PRIMARY"

    instance._capture_manual_discretionary_decision(
        guard=instance.guards[symbol], fill_id="order-1",
        entry_signals=_snapshot(symbol, "YES"),
        filled_at=datetime.now(timezone.utc),
    )
    rows = override_log.read_pool_records(pool_path=instance.discretionary_override_pool_path)
    assert len(rows) == 1


def test_call_put_yes_no_and_no_read_derivation(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_decision_time_spy_bars", lambda _at: ([], 10))
    expectations = {"YES": "FOLLOWED_TOOK", "NO": "OVERRODE_TOOK",
                    "NO READ": "NO_READ_TOOK"}
    for right in ("C", "P"):
        symbol = occ("AMD", 30, right=right)
        instance = _capture_platform(symbol, tmp_path)
        guard = SimpleNamespace(symbol=symbol, kind="option")
        for decision, expected in expectations.items():
            row = instance._capture_manual_discretionary_decision(
                guard=guard, fill_id=f"{right}-{decision}",
                entry_signals=_snapshot(symbol, decision),
                filled_at=datetime.now(timezone.utc),
            )
            assert row["implied_direction"] == ("bullish" if right == "C" else "bearish")
            assert row["operator_action"] == expected


def test_missing_entry_signals_is_no_read_not_imputed(tmp_path, monkeypatch):
    symbol = occ("AMD", 30, right="P")
    instance = _capture_platform(symbol, tmp_path)
    monkeypatch.setattr(ss, "_decision_time_spy_bars", lambda _at: ([], 10))
    row = instance._capture_manual_discretionary_decision(
        guard=SimpleNamespace(symbol=symbol, kind="option"),
        fill_id="missing", entry_signals=None,
        filled_at=datetime.now(timezone.utc),
    )
    assert row["operator_action"] == "NO_READ_TOOK"
    assert row["precheck_status"] == "MISSING"
    assert row["precheck_decision"] is None


def test_capture_failure_cannot_change_fill_or_guard_path(tmp_path, monkeypatch):
    symbol = occ("AMD", 30, right="C")
    instance = _capture_platform(symbol, tmp_path)
    monkeypatch.setattr(ss, "_decision_time_spy_bars", lambda _at: ([], 10))

    def fail_capture(**_kwargs):
        raise OSError("injected capture failure")

    monkeypatch.setattr(override_log, "capture_decision_record", fail_capture)
    cfg = config(symbol)
    cfg["entry_signals"] = _snapshot(symbol, "YES")
    result = instance.open_trade(cfg, manual=True)

    assert result["symbol"] == symbol
    assert len(instance.trading.submits) == 1
    assert instance.guards[symbol].entry == 1.0
    assert any(component == "discretionary_override_capture" and status == "WARNING"
               and "injected capture failure" in event.get("detail", "")
               for component, status, event in instance.health.events)


def test_explicit_skip_is_secondary_and_never_submits_order(tmp_path, monkeypatch):
    symbol = occ("AMD", 30, right="P")
    instance = _capture_platform(symbol, tmp_path)
    monkeypatch.setattr(ss, "_decision_time_spy_bars", lambda _at: ([], 10))
    result = instance.log_discretionary_skip({
        "symbol": symbol,
        "type": "option",
        "request_id": "skip-1",
        "entry_signals": _snapshot(symbol, "NO"),
    })
    assert result == {
        "logged": True,
        "decision_id": "operator-skip:skip-1",
        "operator_action": "OVERRODE_SKIPPED",
        "analysis_role": "SECONDARY",
    }
    assert instance.trading.submits == []
    rows = override_log.read_pool_records(pool_path=instance.discretionary_override_pool_path)
    assert rows[0]["operator_driven"] is True
    assert rows[0]["capture_origin"] == "OPERATOR_SKIP_CONTROL"
