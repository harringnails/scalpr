"""Network-free tests for the paper-only Scalpr climb-add policy."""

import climb_ladder as cl
import scope_policy
from wave_riding import R_NOT_PROFITABLE, R_UNSYNCHRONIZED


SYMBOL = f"SPY{scope_policy.market_date().strftime('%y%m%d')}C00600000"


def obs(now=100.0, underlying=600.0, bid=1.10, ask=1.12, atr=1.0,
        unsynchronized=False):
    return {
        "now": now, "ts": f"2026-08-04T14:00:{int(now) % 60:02d}+00:00",
        "underlying_price": underlying, "option_bid": bid, "option_ask": ask,
        "quote_quality": "good", "quote_age_sec": 0.1, "spread_pct": 1.8,
        "market_open": True, "in_open_window": False, "in_close_window": False,
        "above_vwap_side_ok": True, "trend_ok": True, "atr_value": atr,
        "atr_quality": "FULL", "hard_veto": False,
        "unsynchronized": unsynchronized,
    }


def test_default_off_and_versioned():
    assert cl.feature_enabled() is False
    state = cl.initialize(SYMBOL, 1.0, 1, obs(), {"enabled": True})
    assert state["version"] == "scalpr-climb-add-paper-v0"


def test_persistent_profitable_climb_creates_one_idempotent_intent():
    state = cl.initialize(SYMBOL, 1.0, 1, obs(), {
        "enabled": True, "confirmation_seconds": 10, "max_total_cost_usd": 5000})
    qualifying = obs(now=110, underlying=600.5, bid=2.10, ask=2.12)
    state, intent = cl.evaluate(state, qualifying)
    assert state["status"] == "CONFIRMING" and intent is None
    state, intent = cl.evaluate(state, obs(now=121, underlying=600.6, bid=2.15, ask=2.17))
    assert state["status"] == "ADD_PENDING"
    assert intent["qty"] == 1 and intent["idempotency_key"]


def test_never_averages_down_or_uses_unsynchronized_pair():
    state = cl.initialize(SYMBOL, 1.0, 1, obs(), {"enabled": True})
    state, intent = cl.evaluate(state, obs(now=111, underlying=600.6, bid=.90, ask=.92))
    assert intent is None and R_NOT_PROFITABLE in state["reason_codes"]
    state, intent = cl.evaluate(
        state, obs(now=112, underlying=600.6, bid=1.25, ask=1.27, unsynchronized=True))
    assert intent is None and R_UNSYNCHRONIZED in state["reason_codes"]


def test_fill_rebases_wave_and_enforces_max_adds():
    state = cl.initialize(SYMBOL, 1.0, 1, obs(), {
        "enabled": True, "confirmation_seconds": 0, "max_adds": 1,
        "max_total_cost_usd": 5000})
    state, _ = cl.evaluate(state, obs(now=110, underlying=600.5, bid=2.10, ask=2.12))
    state, intent = cl.evaluate(state, obs(now=111, underlying=600.6, bid=2.15, ask=2.17))
    state = cl.apply_fill(state, {"price": 2.17, "qty": intent["qty"], "ts": "fill"},
                          obs(now=111, underlying=600.6, bid=2.15, ask=2.17))
    assert state["position"]["open_quantity"] == 2
    assert state["position"]["filled_adds"] == 1
    assert state["status"] == "MAX_POSITION_REACHED"


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
            print("PASS", name)
