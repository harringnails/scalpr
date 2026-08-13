"""
Tests for feature_engine.py (scalpr-intel-v0).

Verifies the Phase-1 discipline, not profitability:
  * schema is versioned; formal_cohort_eligible is False everywhere;
  * unwired inputs are explicit nulls (never fabricated), and data_status
    reflects availability;
  * per-contract target-before-stop labels use ONLY forward bars (no leak);
  * the delta-gamma proxy gets the sign right for calls and puts;
  * in-band selection is exactly delta 0.15–0.70 (magnitude);
  * the three scores are independent — a right direction can still be NO_TRADE.

Run: python3 test_feature_engine.py   (no external deps; sources degrade)
"""
import feature_engine as fe


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok: {name}")


def _bar(t, hi, lo, cl, op=None, vol=100):
    return {"t": t, "open": op if op is not None else cl,
            "high": hi, "low": lo, "close": cl, "volume": vol}


# ── schema + stub honesty ───────────────────────────────────────────────────
def test_schema_and_stub_honesty():
    print("Feature record is versioned, non-qualifying, and never fabricates unwired inputs")
    payload = {
        "spot": 176.25, "iv_rank": 40,
        "atm": {"iv": 0.31, "earnings": "2026-08-04"},
        "contracts": [
            {"symbol": "X1", "type": "C", "delta": 0.50, "oi": 1200, "volume": 300,
             "spread_pct": 3.0, "mid": 1.00, "gamma": 0.0, "theta": -0.01,
             "oi_since_prev": 500},
            {"symbol": "X2", "type": "P", "delta": -0.30, "oi": 800, "volume": 50,
             "spread_pct": 4.0, "mid": 0.80, "gamma": 0.0, "oi_since_prev": None},
        ],
    }
    fr = fe.build_feature_record(None, "AMD", workup_payload=payload)
    check("schema_version tagged", fr["schema_version"] == "scalpr-intel-v0")
    check("formal_cohort_eligible False", fr["formal_cohort_eligible"] is False)
    # unwired fields are explicit None, and named in _unwired
    check("skew unavailable (not fabricated)", fr["volatility"]["put_call_skew"] is None)
    check("term-structure unavailable", fr["volatility"]["term_structure_slope"] is None
          and "term_structure_slope" in fr["volatility"]["_unwired"])
    check("side-classified flow unavailable",
          fr["options_flow"]["net_directional_delta"] is None
          and "net_directional_delta" in fr["options_flow"]["_unwired"])
    check("VIX/breadth regime unavailable",
          fr["market_regime"]["vix_percentile"] is None
          and fr["market_regime"]["breadth_score"] is None)
    # available groups DO carry real values
    check("iv_rank passed through", fr["volatility"]["iv_rank"] == 40)
    check("earnings passed through", fr["events"]["earnings_date"] == "2026-08-04")
    check("liquidity computed from chain", fr["liquidity"]["qualified_contracts"] == 2)
    check("net_oi_since_prev only counts non-null", fr["options_flow"]["net_oi_since_prev"] == 500)
    check("liquidity data_status available", fr["liquidity"]["data_status"] == "available")


# ── in-band selection = delta 0.15–0.70 magnitude ──────────────────────────
def test_in_band_selection():
    print("In-band selection is exactly |delta| in [0.15, 0.70]")
    cs = [{"symbol": s, "delta": d} for s, d in
          [("a", 0.10), ("b", 0.15), ("c", 0.50), ("d", 0.70), ("e", 0.80),
           ("f", -0.30), ("g", -0.75), ("h", None)]]
    band = fe.in_band_contracts(cs)
    syms = {c["symbol"] for c in band}
    check("keeps 0.15/0.50/0.70/-0.30", syms == {"b", "c", "d", "f"})
    check("drops 0.10, 0.80, -0.75, None", not syms & {"a", "e", "g", "h"})


# ── per-contract label: no future-data leak + proxy sign (calls) ───────────
def test_label_no_leak_and_call_target():
    print("Call label uses only forward bars; a PAST spike is ignored; target sign correct")
    # decision at minute 10, u0=100. A huge PRE-decision spike at t=5 must not count.
    bars = [_bar(5, 200.0, 100.0, 100.0),     # pre-decision spike -> must be ignored
            _bar(10, 100.0, 100.0, 100.0),    # decision bar -> u0 = 100
            _bar(11, 100.6, 99.9, 100.5),     # future: du_hi=+0.6 -> +0.30 >= +25% target
            _bar(12, 101.0, 100.0, 100.8)]
    c = {"symbol": "C1", "type": "C", "delta": 0.5, "gamma": 0.0, "mid": 1.00}
    lab = fe.label_contract(c, session_minute=10, session_bars=bars)
    check("label available", lab["available"] is True)
    check("target_before_stop True", lab["target_before_stop"] is True)
    check("first hit at a FUTURE bar (minute 1, not the past spike)",
          lab["minutes_to_first_hit"] == 1)
    check("label_basis flagged proxy", lab["label_basis"] == "delta_gamma_proxy")
    check("realized_option_path False", lab["realized_option_path"] is False)
    check("frozen quote captured", lab["frozen_quote"]["symbol"] == "C1")
    check("non-qualifying", lab["formal_cohort_eligible"] is False)


def test_label_call_stop():
    print("Call label: underlying falls -> option stop hit before target")
    bars = [_bar(10, 100.0, 100.0, 100.0),
            _bar(11, 100.0, 99.5, 99.6),      # du_lo=-0.5 -> p=1-0.25=0.75 <= 0.80 stop
            _bar(12, 99.6, 99.0, 99.2)]
    c = {"symbol": "C2", "type": "C", "delta": 0.5, "gamma": 0.0, "mid": 1.00}
    lab = fe.label_contract(c, 10, bars)
    check("stop_before_target True", lab["stop_before_target"] is True)
    check("first_hit == stop", lab["first_hit"] == "stop")
    check("mae negative", lab["mae_pct"] < 0)


def test_label_put_target():
    print("Put label: underlying falls -> put option gains -> target (sign handled)")
    bars = [_bar(10, 100.0, 100.0, 100.0),
            _bar(11, 100.1, 99.5, 99.6)]      # low du=-0.5; put delta -0.5 -> +0.25 target
    c = {"symbol": "P1", "type": "P", "delta": -0.5, "gamma": 0.0, "mid": 1.00}
    lab = fe.label_contract(c, 10, bars)
    check("put target_before_stop True", lab["target_before_stop"] is True)


def test_label_needs_forward_bars():
    print("Label marks itself unavailable when there are no forward bars (no fabrication)")
    bars = [_bar(10, 100.0, 100.0, 100.0)]    # nothing after the decision minute
    c = {"symbol": "C3", "type": "C", "delta": 0.5, "gamma": 0.0, "mid": 1.00}
    lab = fe.label_contract(c, 10, bars)
    check("unavailable without forward bars", lab["available"] is False)
    check("reason names missing forward bars", "forward" in lab["reason"])


def test_label_in_band_covers_all():
    print("label_in_band labels EVERY in-band contract")
    fr = {"underlying": {"session_minute": 10}}
    contracts = [{"symbol": "a", "type": "C", "delta": 0.5, "gamma": 0.0, "mid": 1.0},
                 {"symbol": "b", "type": "C", "delta": 0.2, "gamma": 0.0, "mid": 2.0},
                 {"symbol": "z", "type": "C", "delta": 0.05, "gamma": 0.0, "mid": 0.1}]  # out
    bars = [_bar(10, 100, 100, 100), _bar(11, 101, 100, 100.7)]
    labs = fe.label_in_band(fr, contracts, bars)
    check("labels exactly the 2 in-band contracts", len(labs) == 2)
    check("out-of-band excluded", {l["symbol"] for l in labs} == {"a", "b"})


# ── score separation: direction independent of executability ───────────────
def test_scores_are_independent():
    print("Direction, trade-quality, and executability are separate; a wide spread vetoes")
    good_dir = {
        "ticker": "AMD", "timestamp": "t",
        "underlying": {"above_vwap": True, "vwap_slope_pos": True,
                       "or_broken_up": True, "opening_range_complete": True},
        "market_regime": {"premarket_background": "leaning_bullish",
                          "hmm_available": True, "hmm_state": "trend"},
        "options_flow": {"data_status": "degraded", "net_directional_delta": None},
        "liquidity": {"median_spread_pct": 15.0, "median_open_interest": 1200,
                      "qualified_contracts": 10},
    }
    sc = fe.score_record(good_dir)
    check("direction is LONG (bullish structure)", sc["direction"] == "LONG")
    check("direction_score positive", sc["direction_score"] > 0)
    check("wide spread vetoes -> decision NO_TRADE despite LONG direction",
          sc["decision"] == "NO_TRADE" and "spread_too_wide" in sc["vetoes"])
    check("scores kept independent flag", sc["scores_are_independent"] is True)

    # same direction, now executable
    good_dir["liquidity"] = {"median_spread_pct": 3.0, "median_open_interest": 900,
                             "qualified_contracts": 12}
    sc2 = fe.score_record(good_dir)
    check("executable + directional -> decision LONG", sc2["decision"] == "LONG")
    check("executability high when spread tight", sc2["executability_score"] >= 0.9)
    check("no fabricated probability field", "probability" not in sc2 and "p_up" not in sc2)


def test_neutral_when_conflicting():
    print("Balanced/greyed structure yields NEUTRAL, not a forced side")
    flat = {"ticker": "SPY", "timestamp": "t",
            "underlying": {"above_vwap": True, "vwap_slope_pos": False},
            "market_regime": {"premarket_background": "mixed", "hmm_available": False},
            "options_flow": {"data_status": "unavailable"},
            "liquidity": {"median_spread_pct": 4.0, "median_open_interest": 500,
                          "qualified_contracts": 8}}
    sc = fe.score_record(flat)
    check("neutral direction", sc["direction"] == "NEUTRAL")
    check("decision NEUTRAL (no veto, no side)", sc["decision"] == "NEUTRAL")


if __name__ == "__main__":
    for fn in (test_schema_and_stub_honesty, test_in_band_selection,
               test_label_no_leak_and_call_target, test_label_call_stop,
               test_label_put_target, test_label_needs_forward_bars,
               test_label_in_band_covers_all, test_scores_are_independent,
               test_neutral_when_conflicting):
        fn()
    print("\nALL FEATURE ENGINE TESTS PASSED")
