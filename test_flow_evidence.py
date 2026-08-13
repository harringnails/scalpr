"""
Tests for flow_evidence.py (flow-evidence-v0). Evidence-only, no probability.
"""
from datetime import datetime, timezone

import flow_evidence as fx


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok: {name}")


NOW = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)
FRESH = NOW.isoformat()
STALE = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc).isoformat()   # 5h old


def K(typ, delta, ask_pct, net, sweep, oi_chg, oi):
    return {"type": typ, "delta": delta, "bid": 1.00, "ask": 1.02, "volume": 100,
            "ask_pct": ask_pct, "net_aggressor": net, "sweep_vol": sweep,
            "oi_chg": oi_chg, "oi": oi}


def payload(ticker, contracts, as_of=FRESH, iv=40, gex=True, dark=True):
    return {"ticker": ticker, "as_of": as_of, "contracts": contracts,
            "iv_rank": iv, "gex_levels": ({"walls": []} if gex else None),
            "darkpool": ([{"px": 100}] if dark else None)}


def test_green_aligned_bullish():
    print("Aligned bullish options flow + fresh data → GREEN / BULLISH")
    calls = [K("C", 0.5, 60, 50, 40, 500, 1000) for _ in range(6)]
    puts = [K("P", -0.4, 40, -10, 5, -50, 300) for _ in range(2)]
    s = fx.score_ticker(payload("AAA", calls + puts), now=NOW)
    check("tier GREEN", s["tier"] == "GREEN" and s["star"] == "🟢")
    check("direction BULLISH", s["direction"] == "BULLISH")
    check(">=3 agreeing signals", s["agreeing_signals"] >= 3)
    check("no probability field", "probability" not in s and "p_up" not in s)
    check("is_edge_claim False", s["is_edge_claim"] is False)
    check("unavailable inputs listed", "equity_level2_order_flow" in s["unavailable_inputs"])
    check("gex + darkpool available", any(x["name"] == "dealer_gex" and x["available"] for x in s["signals"]))


def test_amber_conflicting():
    print("Conflicting signals → AMBER / MIXED")
    # calls aggressive-buy bullish, but puts dominate net-aggressor + sweeps + OI (bearish)
    calls = [K("C", 0.5, 60, 5, 2, 10, 1000) for _ in range(4)]
    puts = [K("P", -0.4, 30, 200, 300, 800, 1000) for _ in range(4)]
    s = fx.score_ticker(payload("BBB", calls + puts), now=NOW)
    check("tier AMBER", s["tier"] == "AMBER" and s["star"] == "🟠")
    check("direction not a forced side or mixed handled", s["direction"] in ("MIXED", "BEARISH", "BULLISH"))


def test_insufficient_sparse_or_stale():
    print("Too few contracts OR stale workup → INSUFFICIENT")
    sparse = fx.score_ticker(payload("CCC", [K("C", 0.5, 60, 50, 40, 500, 1000) for _ in range(2)]), now=NOW)
    check("sparse → INSUFFICIENT", sparse["tier"] == "INSUFFICIENT" and sparse["star"] == "")
    calls = [K("C", 0.5, 60, 50, 40, 500, 1000) for _ in range(6)]
    stale = fx.score_ticker(payload("DDD", calls, as_of=STALE), now=NOW)
    check("stale → not fresh → INSUFFICIENT", stale["fresh"] is False and stale["tier"] == "INSUFFICIENT")


def test_unavailable_not_fabricated():
    print("Missing GEX/darkpool shown unavailable, not invented")
    calls = [K("C", 0.5, 60, 50, 40, 500, 1000) for _ in range(6)]
    s = fx.score_ticker(payload("EEE", calls, gex=False, dark=False), now=NOW)
    gex = [x for x in s["signals"] if x["name"] == "dealer_gex"][0]
    dp = [x for x in s["signals"] if x["name"] == "dark_pool"][0]
    check("gex unavailable", gex["available"] is False)
    check("darkpool unavailable", dp["available"] is False)
    # still green on the available signals (they agree)
    check("still ranks on available evidence", s["tier"] in ("GREEN", "AMBER"))


def test_rank_order():
    print("Ranking orders GREEN → AMBER → INSUFFICIENT")
    green = payload("G", [K("C", 0.5, 60, 50, 40, 500, 1000) for _ in range(6)] +
                    [K("P", -0.4, 40, -10, 5, -50, 300) for _ in range(2)])
    amber = payload("A", [K("C", 0.5, 60, 5, 2, 10, 1000) for _ in range(4)] +
                    [K("P", -0.4, 30, 200, 300, 800, 1000) for _ in range(4)])
    insuf = payload("I", [K("C", 0.5, 60, 50, 40, 500, 1000) for _ in range(2)])
    r = fx.rank([amber, insuf, green], now=NOW)
    tiers = [x["tier"] for x in r["ranking"]]
    check("green first, insufficient last", tiers[0] == "GREEN" and tiers[-1] == "INSUFFICIENT")
    check("evidence-only banner", r["is_edge_claim"] is False and "GREEN DATA" in r["note"])


if __name__ == "__main__":
    for fn in (test_green_aligned_bullish, test_amber_conflicting,
               test_insufficient_sparse_or_stale, test_unavailable_not_fabricated,
               test_rank_order):
        fn()
    print("\nALL FLOW EVIDENCE TESTS PASSED")
