"""Network-free safety and selection tests for directional-shadow-v0."""

from datetime import datetime, timezone
import sys
import tempfile
from pathlib import Path

import directional_shadow as ds


NOW = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


def entry(decision, reason="test"):
    return {
        "symbol": "SPY", "decision": decision, "reason": reason,
        "policy_version": "entry-policy-exploratory-v0",
        "data_confidence": 0.8, "missing_inputs": [],
    }


def contracts():
    return [
        {"symbol": "SPY260807C00775000", "type": "C", "clean": True,
         "ask": 4.00, "bid": 3.96, "delta": .60, "spread_pct": 1.0,
         "volume": 5000, "oi": 8000, "dte": 2, "expiry": "2026-08-07", "strike": 775},
        {"symbol": "SPY260807C00770000", "type": "CALL", "clean": True,
         "ask": 3.00, "bid": 2.97, "delta": .46, "spread_pct": 1.0,
         "volume": 1000, "oi": 3000, "dte": 2, "expiry": "2026-08-07", "strike": 770},
        {"symbol": "SPY260807P00770000", "type": "PUT", "clean": True,
         "ask": 2.50, "bid": 2.47, "delta": -.44, "spread_pct": 1.2,
         "volume": 2000, "oi": 4000, "dte": 2, "expiry": "2026-08-07", "strike": 770},
        {"symbol": "SPY260807P00765000", "type": "P", "clean": True,
         "ask": 8.00, "bid": 7.90, "delta": -.45, "spread_pct": 1.25,
         "volume": 9000, "oi": 9000, "dte": 2, "expiry": "2026-08-07", "strike": 765},
    ]


def test_candidate_maps_direction_and_picks_clean_affordable_contract():
    call = ds.build_proposal(entry("LONG_CANDIDATE"), contracts(), now=NOW)
    assert call["decision"] == "CALL"
    assert call["proposed_contract"]["symbol"] == "SPY260807C00770000"
    assert call["proposed_contract"]["estimated_entry_cost_usd"] == 300.0
    put = ds.build_proposal(entry("SHORT_CANDIDATE"), contracts(), now=NOW)
    assert put["decision"] == "PUT"
    assert put["proposed_contract"]["symbol"] == "SPY260807P00770000"


def test_wait_and_veto_default_to_no_trade_without_contract():
    for decision in ("WAIT", "NO_TRADE"):
        p = ds.build_proposal(entry(decision, "blocked"), contracts(), now=NOW)
        assert p["decision"] == "NO_TRADE"
        assert p["proposed_contract"] is None
        assert "blocked" in p["blocking_reasons"]


def test_missing_or_over_budget_contract_fails_closed():
    p = ds.build_proposal(entry("SHORT_CANDIDATE"), [contracts()[-1]], now=NOW)
    assert p["decision"] == "NO_TRADE"
    assert p["blocking_reasons"] == ["NO_CLEAN_AFFORDABLE_CONTRACT"]


def test_log_is_minute_idempotent_and_status_counts():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "shadow.jsonl"
        p = ds.build_proposal(entry("LONG_CANDIDATE"), contracts(), now=NOW)
        assert ds.append_proposal(p, path) is True
        assert ds.append_proposal(p, path) is False
        s = ds.status("SPY", path)
        assert s["samples"] == 1 and s["counts"]["CALL"] == 1


def test_module_has_no_broker_dependency_or_order_path():
    assert "alpaca" not in sys.modules
    assert ds.FORMAL_COHORT_ELIGIBLE is False
    p = ds.build_proposal(entry("LONG_CANDIDATE"), contracts(), now=NOW)
    assert p["shadow_only"] is True and p["broker_orders_reachable"] is False


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
            print("PASS", name)
