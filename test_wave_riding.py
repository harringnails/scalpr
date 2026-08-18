"""
Wave Riding v0 tests — the §26 matrix + replay + failure recovery + isolation.

Verifies the shadow discipline, not profitability. No live orders anywhere.
"""
import os
import inspect
import tempfile

import feature_engine as fe
import wave_config as wc
import wave_riding as wr
import wave_store as wst
import wave_baselines as wb
from wave_config import WaveConfig
from wave_order_adapter import ShadowOrderAdapter, LiveBrokerOrderAdapter

TMP = tempfile.mkdtemp()


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok: {name}")


def cfg(**kw):
    base = dict(enabled=True, confirmation_seconds=10, add_cooldown_seconds=30,
                minimum_option_gain_for_add_pct=10, max_adds=2, max_total_contracts=3,
                require_profit_funded_add=False, require_vwap_alignment=True,
                slippage_allowance=0.02, add_trigger_atr_fraction=0.40)
    base.update(kw)
    return WaveConfig.from_dict(base)


def obs(now, u, bid, ask, **kw):
    mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
    o = {"now": now, "ts": f"2026-07-27T15:{int(now) // 60 % 60:02d}:{int(now) % 60:02d}+00:00",
         "underlying_price": u, "option_bid": bid, "option_ask": ask,
         "option_mid": mid, "option_last": mid,
         "spread_pct": (round((ask - bid) / mid * 100, 3) if mid else None),
         "quote_age_sec": 0, "quote_quality": "ok", "market_open": True,
         "in_open_window": False, "in_close_window": False,
         "above_vwap_side_ok": True, "trend_ok": True,
         "atr_value": 1.0, "atr_quality": "FULL", "hard_veto": False,
         "session_minute": 30, "side": "CALL", "underlying_symbol": "SPY",
         "contract_symbol": "C1"}
    o.update(kw)
    return o


def seed(config, side="CALL", u=100.0, bid=1.00, ask=1.02):
    r = wb.WaveRunner(config)
    o0 = obs(0, u, bid, ask, side=side)
    pos, fill = r.seed("p", side, "SPY", "C1", o0)
    return r, pos, fill


# ── config validation ──────────────────────────────────────────────────────
def test_config_invariants():
    print("Config: averaging-down impossible, caps required, shadow forced")
    try:
        WaveConfig(allow_averaging_down=True).validate(); bad = False
    except ValueError:
        bad = True
    check("allow_averaging_down=true rejected", bad)
    try:
        WaveConfig.from_dict({"max_total_contracts": 0}); bad2 = False
    except ValueError:
        bad2 = True
    check("missing/zero cap rejected", bad2)
    check("default is shadow + live disabled",
          WaveConfig().shadow_mode and not WaveConfig().live_add_orders_enabled)
    check("feature flag default OFF", wc.feature_enabled() is False)


# ── CALL behavior ───────────────────────────────────────────────────────────
def test_call_below_trigger_no_add():
    print("Call below ATR trigger → no add")
    r, pos, fill = seed(cfg())
    check("seeded MONITORING w/ 1 contract", pos["state"] == wr.MONITORING_WAVE and pos["open_quantity"] == 1)
    pos, dec = r.step(pos, obs(1, 100.2, 1.05, 1.07))     # move 0.2 < 0.4
    check("no add, move insufficient", dec["intended_action"] == wr.ACT_NONE
          and wr.R_UNDERLYING_SHORT in dec["reason_codes"])


def test_call_one_tick_no_add_then_persist_adds():
    print("Call one-tick cross → no add; persisted cross → add")
    r, pos, fill = seed(cfg())
    pos, dec = r.step(pos, obs(2, 100.6, 1.20, 1.22))     # eligible → CONFIRMING only
    check("one tick → CONFIRMING, no submit", pos["state"] == wr.ADD_CONFIRMING
          and dec["intended_action"] == wr.ACT_NONE)
    pos, dec = r.step(pos, obs(13, 100.6, 1.20, 1.22))    # elapsed 11s ≥ 10
    check("persisted → SUBMIT_ADD", dec["intended_action"] == wr.ACT_SUBMIT_ADD)
    check("qty grew to 2", pos["open_quantity"] == 2)
    check("avg cost updated", abs(pos["weighted_average_option_cost"] - 1.14) < 1e-6)
    check("anchor updated to fill underlying", pos["last_wave_anchor_underlying_price"] == 100.6)
    check("cooldown set", pos["state"] == wr.COOLDOWN and pos["cooldown_until"] == 43)


def test_call_confirmation_resets_on_failure():
    print("Call falls below trigger during confirmation → timer resets")
    r, pos, fill = seed(cfg())
    pos, dec = r.step(pos, obs(2, 100.6, 1.20, 1.22))
    check("confirming", pos["state"] == wr.ADD_CONFIRMING)
    # underlying falls below the ADD trigger, but option giveback stays < 2.5%
    # (so no reversal exit) — isolates the confirmation-reset path
    pos, dec = r.step(pos, obs(5, 100.1, 1.185, 1.19))
    check("reset to MONITORING", pos["state"] == wr.MONITORING_WAVE
          and pos["confirm_started_at"] is None and "CONFIRMATION_RESET" in dec["reason_codes"])


def test_call_second_add_then_max():
    print("Call continues after cooldown → second add → MAX_POSITION_REACHED")
    r, pos, fill = seed(cfg())
    pos, _ = r.step(pos, obs(2, 100.6, 1.20, 1.22))
    pos, _ = r.step(pos, obs(13, 100.6, 1.20, 1.22))      # add #1 → COOLDOWN until 43
    pos, dec = r.step(pos, obs(20, 101.5, 1.45, 1.47))    # still cooldown
    check("cooldown blocks", pos["state"] == wr.COOLDOWN)
    pos, _ = r.step(pos, obs(44, 101.5, 1.45, 1.47))      # eligible → CONFIRMING
    pos, dec = r.step(pos, obs(55, 101.5, 1.45, 1.47))    # add #2
    check("second add filled", pos["open_quantity"] == 3 and dec["intended_action"] == wr.ACT_SUBMIT_ADD)
    check("max position reached", pos["state"] == wr.MAX_POSITION_REACHED)
    pos, dec = r.step(pos, obs(90, 103.0, 1.80, 1.82))    # no more adds
    check("no adds past max", dec["intended_action"] == wr.ACT_NONE)


def test_call_reversal_whole_qty_exit():
    print("Call reverses from peak → whole-position exit")
    r, pos, fill = seed(cfg(add_trigger_atr_fraction=100))   # adds unreachable; isolate exit
    pos, _ = r.step(pos, obs(1, 101, 1.30, 1.32))            # peak exec 130
    check("peak set on executable bid", pos["peak_executable_value_usd"] == 130.0)
    pos, dec = r.step(pos, obs(2, 101, 1.26, 1.28))          # giveback 3.08% ≥ 2.5%
    check("reversal exit whole position", pos["state"] == wr.CLOSED and pos["open_quantity"] == 0)


# ── PUT behavior (mirror through the same engine) ───────────────────────────
def test_put_add_and_reversal():
    print("Put mirrors call: falling underlying + rising put value adds, then reverses")
    r, pos, fill = seed(cfg(), side="PUT", u=100.0, bid=1.00, ask=1.02)
    # underlying FALLS 0.6 (directional_move = -(-0.6)=+0.6), put bid rises to 1.20
    pos, _ = r.step(pos, obs(2, 99.4, 1.20, 1.22, side="PUT"))
    pos, dec = r.step(pos, obs(13, 99.4, 1.20, 1.22, side="PUT"))
    check("put add fires on falling underlying", pos["open_quantity"] == 2)
    # reverse: put value gives back from peak
    r2, pos2, _ = seed(cfg(add_trigger_atr_fraction=100), side="PUT")
    pos2, _ = r2.step(pos2, obs(1, 99, 1.30, 1.32, side="PUT"))
    pos2, dec2 = r2.step(pos2, obs(2, 99, 1.26, 1.28, side="PUT"))
    check("put reversal exits whole position", pos2["state"] == wr.CLOSED)


# ── no averaging down ───────────────────────────────────────────────────────
def test_no_averaging_down():
    print("Never adds to a losing / unconfirmed / unprofitable position")
    e = wr.WaveEngine(cfg())
    r, pos, fill = seed(cfg())
    # underlying against
    ok, reasons, _ = e.evaluate_add(pos, obs(1, 99.0, 0.90, 0.92))
    check("underlying against → blocked", not ok and wr.R_UNDERLYING_SHORT in reasons)
    # option cheaper than last add exec bid
    ok, reasons, _ = e.evaluate_add(pos, obs(1, 100.6, 0.95, 0.97))
    check("option cheaper → not confirmed", not ok and wr.R_OPTION_NOT_CONFIRMED in reasons)
    # profitable required: high cost basis, exec below it
    p2 = dict(pos); p2["total_cost_basis_usd"] = 200.0; p2["last_add_exec_bid"] = 1.00
    ok, reasons, _ = e.evaluate_add(p2, obs(1, 100.6, 1.20, 1.22))   # exec 120 < 200
    check("unprofitable whole position → blocked", not ok and wr.R_NOT_PROFITABLE in reasons)


# ── limits ──────────────────────────────────────────────────────────────────
def test_limits_block_adds():
    print("Max adds / contracts / cost / profit-funded all block")
    e = wr.WaveEngine(cfg())
    r, pos, fill = seed(cfg())
    p = dict(pos); p["filled_adds"] = 2
    ok, reasons, _ = e.evaluate_add(p, obs(1, 100.6, 1.20, 1.22))
    check("max adds blocks", wr.R_MAX_ADDS in reasons)
    p = dict(pos); p["open_quantity"] = 3
    ok, reasons, _ = e.evaluate_add(p, obs(1, 100.6, 1.20, 1.22))
    check("max contracts blocks", wr.R_MAX_CONTRACTS in reasons)
    e2 = wr.WaveEngine(cfg(max_total_cost_usd=50))
    ok, reasons, _ = e2.evaluate_add(pos, obs(1, 100.6, 1.20, 1.22))
    check("projected cost cap blocks", wr.R_MAX_COST in reasons)
    e3 = wr.WaveEngine(cfg(require_profit_funded_add=True, required_profit_coverage_ratio=0.5))
    ok, reasons, _ = e3.evaluate_add(pos, obs(1, 100.6, 1.20, 1.22))
    check("profit-funded gate blocks small profit", wr.R_PROFIT_FUNDED in reasons)


# ── quote protection ────────────────────────────────────────────────────────
def test_quote_protection():
    print("Stale / crossed / missing / wide / last-trade-spike all block adds")
    e = wr.WaveEngine(cfg())
    r, pos, fill = seed(cfg())
    good = obs(1, 100.6, 1.20, 1.22)
    for q in ("stale", "crossed", "missing", "unusable"):
        ok, reasons, _ = e.evaluate_add(pos, {**good, "quote_quality": q})
        check(f"{q} quote blocks", not ok and wr.R_QUOTE_BAD in reasons)
    ok, reasons, _ = e.evaluate_add(pos, {**good, "option_bid": None})
    check("missing bid blocks", not ok and wr.R_QUOTE_BAD in reasons)
    ok, reasons, _ = e.evaluate_add(pos, {**good, "spread_pct": 6.0})
    check("wide spread blocks", not ok and wr.R_SPREAD_WIDE in reasons)
    ok, reasons, _ = e.evaluate_add(pos, {**good, "quote_age_sec": 30})
    check("stale-age blocks", not ok and wr.R_QUOTE_OLD in reasons)
    # last-trade spike but bid unchanged (no option confirmation)
    spike = obs(1, 100.6, 1.00, 1.02, option_last=5.0)   # bid == last_add_exec_bid
    ok, reasons, _ = e.evaluate_add(pos, spike)
    check("last-trade spike w/o bid confirm → no add", not ok and wr.R_OPTION_NOT_CONFIRMED in reasons)


# ── ATR: warm-up + frozen goalpost ──────────────────────────────────────────
def test_atr_warmup_and_frozen_goalpost():
    print("ATR warm-up blocks; frozen goalpost does not move when ATR expands")
    e = wr.WaveEngine(cfg())
    r, pos, fill = seed(cfg())
    ok, reasons, _ = e.evaluate_add(pos, obs(1, 100.6, 1.20, 1.22, atr_value=None, atr_quality="WARMUP_BLOCKED"))
    check("warm-up blocks add", not ok and wr.R_ATR_WARMUP in reasons)
    # frozen atr = 1.0 (from seed); expanding live ATR to 5.0 must NOT raise the goalpost
    ok, reasons, metrics = e.evaluate_add(pos, obs(1, 100.6, 1.20, 1.22, atr_value=5.0))
    check("required move uses FROZEN atr (0.4), not live 5.0", metrics["required_directional_move"] == 0.4)
    check("move 0.6 clears frozen goalpost → eligible", ok)


# ── exit: threshold inclusivity + partial liquidation + pending cancel ──────
def test_exit_threshold_and_partial():
    print("Giveback threshold inclusive; partial liquidation continues to zero")
    r, pos, fill = seed(cfg(add_trigger_atr_fraction=100), bid=1.00, ask=1.002)  # peak 100
    pos, dec = r.step(pos, obs(1, 100, 0.98, 0.982))     # giveback 2.0% < 2.5 → hold
    check("below threshold → no exit", pos["state"] != wr.CLOSED)
    pos, dec = r.step(pos, obs(2, 100, 0.975, 0.977))    # giveback exactly 2.5% → exit (inclusive)
    check("giveback == threshold exits (inclusive)", pos["state"] == wr.CLOSED)
    # partial liquidation semantics
    e = wr.WaveEngine(cfg())
    p = dict(pos); p["open_quantity"] = 3; p["weighted_average_option_cost"] = 1.0; p["state"] = wr.LIQUIDATING
    p = e.apply_exit_fill(p, {"price": 1.1, "qty": 2, "ts": "t"})
    check("partial → PARTIALLY_LIQUIDATED, 1 left", p["state"] == wr.PARTIALLY_LIQUIDATED and p["open_quantity"] == 1)
    p = e.apply_exit_fill(p, {"price": 1.1, "qty": 1, "ts": "t"})
    check("remainder → CLOSED at zero", p["state"] == wr.CLOSED and p["open_quantity"] == 0)


def test_pending_add_cancelled_on_exit():
    print("Emergency exit while confirming cancels the pending add and liquidates")
    r, pos, fill = seed(cfg())
    pos, _ = r.step(pos, obs(2, 100.6, 1.20, 1.22))       # ADD_CONFIRMING
    check("confirming", pos["state"] == wr.ADD_CONFIRMING)
    pos, dec = r.step(pos, obs(3, 100.6, 1.20, 1.22, emergency_exit=True))
    check("emergency exit prioritized", dec["reason_codes"][0] == wr.X_EMERGENCY)
    check("exited / no pending add", pos["state"] == wr.CLOSED and not pos.get("pending_order_key"))


# ── duplicate protection (idempotency) ──────────────────────────────────────
def test_idempotency_dedupe():
    print("Duplicate order key never double-fills")
    path = os.path.join(TMP, "orders.jsonl")
    key = "K1"
    check("first append writes", wst.append_order({"idempotency_key": key, "filled": True}, path) is True)
    check("second append is a no-op", wst.append_order({"idempotency_key": key, "filled": True}, path) is False)
    check("order_seen True", wst.order_seen(key, path) is True)


# ── restart recovery ────────────────────────────────────────────────────────
def test_restart_recovery():
    print("Restart drops unfilled pending add + requires fresh confirmation (no auto-add)")
    base = os.path.join(TMP, "pos")
    orders = os.path.join(TMP, "orders_r.jsonl")
    r, pos, fill = seed(cfg())
    pos, _ = r.step(pos, obs(2, 100.6, 1.20, 1.22))       # ADD_CONFIRMING
    pos["pending_order_key"] = "NEVER_FILLED"
    pos["state"] = wr.ADD_PENDING
    wst.save_position(pos, base)
    recovered = wst.reconstruct("p", base, orders)
    check("pending unfilled dropped", recovered["pending_order_key"] is None)
    check("state reverted to MONITORING", recovered["state"] == wr.MONITORING_WAVE)
    check("awaiting fresh confirmation", recovered["awaiting_fresh_confirmation"] is True)
    # first observe after restart must NOT immediately add
    e = wr.WaveEngine(cfg())
    dec = e.observe(recovered, obs(100, 100.6, 1.20, 1.22))
    check("no immediate add on restart", dec["intended_action"] == wr.ACT_NONE
          and "AWAIT_FRESH_CONFIRMATION" in dec["reason_codes"])


# ── live adapter is unreachable ─────────────────────────────────────────────
def test_live_adapter_unreachable():
    print("Live broker adapter raises — no live order path exists in v0")
    live = LiveBrokerOrderAdapter()
    for fn in (lambda: live.fill_add({}, 1, cfg()), lambda: live.fill_exit({}, 1, cfg())):
        try:
            fn(); raised = False
        except NotImplementedError:
            raised = True
        check("live fill raises NotImplementedError", raised)


# ── three-track same seed ───────────────────────────────────────────────────
def test_three_tracks_same_seed():
    print("Baselines A/B and Wave Riding share ONE seed / initial fill")
    observations = [obs(0, 100, 1.00, 1.02)]
    for i in range(1, 40):
        observations.append(obs(i, 100 + i * 0.05, 1.00 + i * 0.02, 1.02 + i * 0.02))
    tracks = wb.build_tracks(cfg(), observations)
    entry = tracks["seed"]["initial_fill"]["price"]
    check("same entry cost across tracks",
          tracks["baseline_a"]["entry_cost_per_contract"] == entry
          and tracks["baseline_b"]["entry_cost_per_contract"] == entry
          and tracks["wave_riding"]["entry_cost_per_contract"] == entry)
    check("all three tracks present", all(k in tracks for k in ("baseline_a", "baseline_b", "wave_riding")))
    check("comparison computed", "comparison" in tracks)


# ── no look-ahead (determinism: output depends only on pos+obs) ─────────────
def test_no_look_ahead_determinism():
    print("Engine decision depends only on the current observation (no future data)")
    e = wr.WaveEngine(cfg())
    r, pos, fill = seed(cfg())
    o = obs(2, 100.6, 1.20, 1.22)
    d1 = e.observe(pos, o)
    d2 = e.observe(pos, o)
    check("same pos+obs → identical decision",
          fe.canonical_hash(d1["audit_payload"]) == fe.canonical_hash(d2["audit_payload"]))


# ── Standard-mode isolation ─────────────────────────────────────────────────
def test_standard_mode_isolation():
    print("Wave Riding imports never pull in scalp_server / touch Standard mode")
    for module in (fe, wc, wr, wst, wb):
        check(f"{module.__name__}: no scalp_server import",
              "import scalp_server" not in inspect.getsource(module))
    check("feature flag off by default", wc.feature_enabled() is False)
    check("shadow adapter is not live", ShadowOrderAdapter().live is False)


if __name__ == "__main__":
    for fn in (test_config_invariants, test_call_below_trigger_no_add,
               test_call_one_tick_no_add_then_persist_adds, test_call_confirmation_resets_on_failure,
               test_call_second_add_then_max, test_call_reversal_whole_qty_exit,
               test_put_add_and_reversal, test_no_averaging_down, test_limits_block_adds,
               test_quote_protection, test_atr_warmup_and_frozen_goalpost,
               test_exit_threshold_and_partial, test_pending_add_cancelled_on_exit,
               test_idempotency_dedupe, test_restart_recovery, test_live_adapter_unreachable,
               test_three_tracks_same_seed, test_no_look_ahead_determinism,
               test_standard_mode_isolation):
        fn()
    print("\nALL WAVE RIDING TESTS PASSED")
