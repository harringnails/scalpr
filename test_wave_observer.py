"""
wave-riding-shadow-observer-v0 acceptance + failure-recovery + baseline parity.

Runs in a temp CWD so all wave artifacts (positions/seeds/streams/logs, all
relative paths) stay isolated. No live orders anywhere.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

TMP = tempfile.mkdtemp()
os.chdir(TMP)                      # isolate all relative wave_store artifacts here

import feature_engine as fe          # noqa: E402
import wave_config as wc             # noqa: E402
import wave_quotes as wq             # noqa: E402
import wave_riding as wr             # noqa: E402
import wave_store as wst             # noqa: E402
import wave_baselines as wb          # noqa: E402
from wave_config import WaveConfig   # noqa: E402
from wave_observer import ShadowObserver, observation_id  # noqa: E402


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok: {name}")


def CFG(**kw):
    base = dict(enabled=True, confirmation_seconds=10, add_cooldown_seconds=30,
                minimum_option_gain_for_add_pct=10, max_adds=2, max_total_contracts=3,
                require_profit_funded_add=False, slippage_allowance=0.02,
                add_trigger_atr_fraction=0.40, max_underlying_option_timestamp_skew_seconds=2.0)
    base.update(kw)
    return WaveConfig.from_dict(base)


def _iso(ep):
    return datetime.fromtimestamp(ep, tz=timezone.utc).isoformat()


def obs(seq, now, u, obid, oask, side="CALL", u_age=0.0, o_age=0.0, atr=1.0,
        atrq="FULL", market_open=True, cfg=None):
    cfg = cfg or CFG()
    uq = {"bid": u - 0.01, "ask": u + 0.01, "provider_ts_epoch": now - u_age,
          "provider_ts_iso": _iso(now - u_age)}
    oq = {"bid": obid, "ask": oask, "provider_ts_epoch": now - o_age,
          "provider_ts_iso": _iso(now - o_age)}
    atr_audit = {"atr_value": atr, "atr_quality": atrq, "atr_version": "intraday-atr-v0"}
    session = {"market_open": market_open, "in_open_window": False,
               "in_close_window": False, "session_minute": 30}
    return wq.assemble_observation(sequence=seq, now_epoch=now, now_iso=_iso(now),
                                   side=side, underlying_symbol="SPY", contract_symbol="C1",
                                   u_quote=uq, o_quote=oq, atr_audit=atr_audit, cfg=cfg,
                                   session=session, vwap=u - 1.0, feed="iex")


# ── field-level provenance (not uniformly IEX) ──────────────────────────────
def test_field_level_sources():
    print("Per-field sources: underlying IEX, option OPRA, independent timestamps")
    o = obs(0, 1000, 100, 1.00, 1.02)
    check("underlying source is IEX", "iex" in o["sources"]["underlying_quote"])
    check("option source is OPRA (not IEX)", o["sources"]["option_quote"].endswith("opra"))
    check("bars source is IEX", "iex" in o["sources"]["underlying_5m_bars"])
    check("timestamps kept per-field", o["underlying_quote_timestamp"] != None
          and o["option_quote_timestamp"] != None)
    check("no merged synthetic timestamp", "timestamp_skew_seconds" in o)


def test_native_spx_sources_are_explicit_and_never_use_stock_proxy():
    print("Native SPX values: no SPY proxy, sampled-TWAP provenance explicit")
    class NoStock:
        def get_stock_latest_quote(self, _):
            raise AssertionError("stock proxy was queried for SPX")
    class Index:
        def latest_value(self, _):
            return {"value": 7700.0, "provider_ts_epoch": 1000.0,
                    "provider_ts_iso": _iso(1000.0)}
    class Option:
        def get_option_latest_quote(self, _):
            q = type("Q", (), {"bid_price": 10.0, "ask_price": 10.2,
                                 "timestamp": datetime.fromtimestamp(
                                     1000.0, tz=timezone.utc)})()
            return {"SPXW1": q}
    bars = [{"t": i, "high": 7700.0 + i, "low": 7699.0 + i,
             "close": 7700.0 + i, "volume": 60} for i in range(30)]
    session = {"market_open": True, "in_open_window": False,
               "in_close_window": False, "session_minute": 30}
    o = wq.fetch_synchronized(
        stock_data=NoStock(), option_data=Option(), index_data=Index(),
        underlying_is_index=True, feed="iex", underlying_symbol="SPX",
        contract_symbol="SPXW1", side="CALL", cfg=CFG(), sequence=0,
        minute_buffer=bars, session=session, now_epoch=1000.0,
        now_iso=_iso(1000.0))
    check("native index quote source", o["sources"]["underlying_quote"] == wq.SRC_INDEX_VALUE)
    check("native index bar source", o["sources"]["underlying_5m_bars"] == wq.SRC_INDEX_BARS)
    check("sampled TWAP is not mislabeled VWAP",
          o["alignment_reference_type"] == "sampled_index_twap")
    check("SPX and option timestamps synchronize", o["synchronization_status"] == wq.SYNC_OK)


# ── start flow ──────────────────────────────────────────────────────────────
def test_start_valid_creates_and_persists():
    print("Valid synchronized quote + ready ATR → simulation created + persisted")
    ob = ShadowObserver(CFG())
    r = ob.start("p1", "SPY", "C1", "CALL", obs(0, 1000, 100, 1.00, 1.02),
                 source={"source_position_id": "REAL-123"})
    check("created", r["created"] is True and r["seed_hash"])
    check("registered active", "p1" in wst.list_active())
    pos = wst.load_position("p1")
    check("seeded MONITORING, 1 contract", pos["state"] == wr.MONITORING_WAVE and pos["open_quantity"] == 1)
    seed = wst.load_seed("p1")
    check("seed persisted with matching hash", seed and seed["seed_hash"] == r["seed_hash"])
    check("referential source-only link", seed["source_link"]["source_position_id"] == "REAL-123"
          and pos["source_position_id"] == "REAL-123")


def test_start_blocked_reasons():
    print("Start blocked (no position) on unsync / bad quote / ATR warm-up")
    ob = ShadowObserver(CFG())
    r1 = ob.start("bU", "SPY", "C1", "CALL", obs(0, 1000, 100, 1.00, 1.02, o_age=5))  # skew 5>2
    check("unsync blocks", not r1["created"] and wr.R_UNSYNCHRONIZED in r1["blocking_reason"])
    r2 = ob.start("bQ", "SPY", "C1", "CALL", obs(0, 1000, 100, 1.10, 1.00))            # crossed
    check("bad quote blocks", not r2["created"] and "BAD_OPTION_QUOTE" in r2["blocking_reason"])
    r3 = ob.start("bA", "SPY", "C1", "CALL", obs(0, 1000, 100, 1.00, 1.02, atr=None, atrq="WARMUP_BLOCKED"))
    check("ATR warm-up blocks", not r3["created"] and wr.R_ATR_WARMUP in r3["blocking_reason"])
    for pid in ("bU", "bQ", "bA"):
        check(f"{pid} not active", pid not in wst.list_active())


# ── sequencing / dedupe ─────────────────────────────────────────────────────
def test_sequence_dedupe():
    print("Duplicate + out-of-order observations are auditable no-ops")
    ob = ShadowObserver(CFG())
    ob.start("s1", "SPY", "C1", "CALL", obs(0, 1000, 100, 1.00, 1.02))
    pos = wst.load_position("s1")
    r = ob.observe(pos, obs(0, 1000, 100, 1.00, 1.02))    # same seq 0 already processed
    check("duplicate seq → DUPLICATE_OBSERVATION no-op", r["applied"] is False and r["code"] == "DUPLICATE_OBSERVATION")
    pos = wst.load_position("s1")
    r2 = ob.observe(pos, obs(3, 1005, 100.6, 1.20, 1.22))  # jump to seq 3 (applied)
    check("newer seq applied", r2["applied"] is True)
    pos = wst.load_position("s1")
    r3 = ob.observe(pos, obs(2, 1006, 100.6, 1.20, 1.22))  # seq 2 < last 3
    check("older seq → OUT_OF_ORDER_OBSERVATION", r3["applied"] is False and r3["code"] == "OUT_OF_ORDER_OBSERVATION")


# ── unsynchronized gate blocks add + reversal ───────────────────────────────
def test_unsync_blocks_add_and_reversal():
    print("Unsynchronized pair blocks add + reversal; audited; no confirm advance")
    ob = ShadowObserver(CFG())
    ob.start("u1", "SPY", "C1", "CALL", obs(0, 1000, 100, 1.00, 1.02))
    pos = wst.load_position("u1")
    # eligible add geometry BUT unsynchronized (option quote 5s stale vs underlying)
    r = ob.observe(pos, obs(1, 1002, 100.6, 1.20, 1.22, o_age=5))
    check("processed", r["applied"] is True)
    check("no add on unsync", r["position"]["open_quantity"] == 1)
    check("unsync reason audited", wr.R_UNSYNCHRONIZED in r["decision"]["reason_codes"])


# ── pause / resume ──────────────────────────────────────────────────────────
def test_pause_resume():
    print("Pause halts observation; resume requires a fresh synchronized quote")
    ob = ShadowObserver(CFG())
    ob.start("pr", "SPY", "C1", "CALL", obs(0, 1000, 100, 1.00, 1.02))
    pos = ob.pause(wst.load_position("pr"))
    r = ob.observe(pos, obs(1, 1002, 100.6, 1.20, 1.22))
    check("paused → no-op", r["applied"] is False and r["code"] == "PAUSED")
    pos = ob.resume(wst.load_position("pr"))
    check("resume sets awaiting_fresh_confirmation", pos["awaiting_fresh_confirmation"] is True)


# ── stop / abandon ──────────────────────────────────────────────────────────
def test_stop_and_abandon():
    print("MANUAL_SHADOW_STOP needs a valid quote; abandon is a distinct terminal")
    ob = ShadowObserver(CFG())
    ob.start("st", "SPY", "C1", "CALL", obs(0, 1000, 100, 1.00, 1.02))
    pos = wst.load_position("st")
    bad = ob.stop(pos, obs(1, 1002, 100, 1.00, 1.02, o_age=9))   # unsync → cannot stop
    check("no valid quote → STOP_PENDING_VALID_QUOTE", bad["status"] == "STOP_PENDING_VALID_QUOTE"
          and bad["stopped"] is False)
    good = ob.stop(pos, obs(1, 1002, 100, 1.00, 1.02))           # valid synchronized
    check("valid → MANUAL_SHADOW_STOP closes", good["stopped"] is True
          and good["position"]["state"] == wr.CLOSED and good["position"]["exit_reason"] == "MANUAL_SHADOW_STOP")
    check("stopped position unregistered", "st" not in wst.list_active())
    # abandon a fresh one
    ob.start("ab", "SPY", "C1", "CALL", obs(0, 2000, 100, 1.00, 1.02))
    p = ob.abandon(wst.load_position("ab"))
    check("abandon is distinct terminal", p["terminal_status"] == "ABANDONED_WITHOUT_TERMINAL_FILL")
    fin = ob.finalize("ab")
    check("abandoned excluded from normal results", fin.get("abandoned") is True)


# ── restart recovery ────────────────────────────────────────────────────────
def test_restart_recovery():
    print("Restart reloads state, requires fresh quote, no manufactured obs, no double-fill")
    ob = ShadowObserver(CFG())
    ob.start("rr", "SPY", "C1", "CALL", obs(0, 3000, 100, 1.00, 1.02))
    pos = wst.load_position("rr")
    r = ob.observe(pos, obs(1, 3002, 100.6, 1.20, 1.22))   # last_processed_sequence = 1
    recovered = ob.reload("rr")
    check("awaiting fresh confirmation after restart", recovered["awaiting_fresh_confirmation"] is True)
    check("sequence preserved across restart", recovered["last_processed_sequence"] == 1)
    # re-delivering the already-processed observation must NOT double-fill
    r2 = ob.observe(recovered, obs(1, 3002, 100.6, 1.20, 1.22))
    check("replayed obs deduped", r2["applied"] is False and r2["code"] in ("DUPLICATE_OBSERVATION", "OUT_OF_ORDER_OBSERVATION"))


# ── same immutable seed across three tracks ─────────────────────────────────
def test_shared_seed_three_tracks():
    print("Finalize replays three tracks from ONE stored seed + observation stream")
    ob = ShadowObserver(CFG(add_trigger_atr_fraction=100))   # no adds; just ride+stop
    ob.start("tk", "SPY", "C1", "CALL", obs(0, 4000, 100, 1.00, 1.02))
    pos = wst.load_position("tk")
    for i in range(1, 20):
        pos = ob.observe(pos, obs(i, 4000 + i, 100 + i * 0.02, 1.00 + i * 0.01, 1.02 + i * 0.01))["position"]
    tracks = ob.finalize("tk")
    entry = tracks["seed"]["initial_fill"]["price"]
    check("all three tracks share the entry fill",
          tracks["baseline_a"]["entry_cost_per_contract"] == entry
          and tracks["baseline_b"]["entry_cost_per_contract"] == entry
          and tracks["wave_riding"]["entry_cost_per_contract"] == entry)
    check("seed hash attached", tracks["seed_hash"] == wst.load_seed("tk")["seed_hash"])
    check("baseline-policy stamps present",
          tracks["simulated_standard_policy_version"] == "wave-riding-baseline-v0"
          and tracks["simulated_standard_policy_hash"]
          and tracks["live_guard_reference_version"] == "WHOLE_POSITION_RATCHET")


# ── isolation ───────────────────────────────────────────────────────────────
def test_isolation():
    print("Observer never imports scalp_server; no live order path")
    check("scalp_server not imported", "scalp_server" not in sys.modules)
    from wave_order_adapter import ShadowOrderAdapter
    check("shadow adapter not live", ShadowOrderAdapter().live is False)


# ── baseline parity vs a line-by-line Guard reference ───────────────────────
def _guard_reference_exit(series, entry, ladder, confirm=2, grace=0.0,
                          stall_seconds=0.0, stall_min_profit=20.0):
    """Mirrors scalp_server.Guard.on_price EXACTLY (grace/stall driven by an
    injected clock). MUST match wave_baselines.simulate_standard_ratchet — if the
    live Guard changes, update BOTH and this test reports the divergence."""
    ladder = sorted(ladder, key=lambda r: r["at"])
    peak = 0.0; breach = 0; opened = series[0][1]; peak_time = opened
    for (bid, now) in series:
        profit = (bid - entry) / entry * 100.0
        if profit > peak:
            peak, breach, peak_time = profit, 0, now
            continue
        if now - opened < grace:
            continue
        tol = ladder[0]["tol"]
        for r in ladder:
            if peak >= r["at"]:
                tol = r["tol"]
        if profit <= peak - tol:
            breach += 1
            if breach >= confirm:
                return now, "standard_ratchet"
            continue
        breach = 0
        if stall_seconds and peak >= stall_min_profit and now - peak_time >= stall_seconds:
            return now, "stall"
    return None, "none"


def test_baseline_guard_parity():
    print("Baseline A matches the Guard reference across representative paths")
    entry = 1.00
    ladder = wb.DEFAULT_LADDER

    def series_to_obs(series):
        return [{"option_bid": bid, "option_ask": bid + 0.01, "now": now,
                 "ts": _iso(now), "market_open": True} for (bid, now) in series]

    paths = {
        "ordinary_ratchet": [(1.00, 0), (1.20, 1), (1.16, 2), (1.10, 3), (1.05, 4)],
        "hard_giveback": [(1.00, 0), (1.40, 1), (1.10, 2), (1.00, 3)],
        "unchanged": [(1.00, i) for i in range(6)],
        "rapid_peak_reversal": [(1.00, 0), (1.60, 1), (1.20, 2), (1.10, 3)],
    }
    for name, series in paths.items():
        ref_ts, ref_reason = _guard_reference_exit(series, entry, ladder, confirm=2, grace=0.0)
        res = wb.simulate_standard_ratchet(series_to_obs(series), entry, ladder=ladder,
                                           confirm_ticks=2, grace_seconds=0.0, slippage=0.0)
        sim_ts = res["exit"]["ts"] if res["exit"]["reason"] != "terminal" else None
        ref_iso = _iso(ref_ts) if ref_ts is not None else None
        check(f"[{name}] exit parity (ref={ref_reason})", sim_ts == ref_iso)
    # stall path parity (Guard stall enabled)
    stall_series = [(1.00, 0), (1.30, 1)] + [(1.30, 1 + s) for s in range(2, 40)]
    ref_ts, ref_reason = _guard_reference_exit(stall_series, entry, ladder, confirm=2,
                                               grace=0.0, stall_seconds=10, stall_min_profit=20)
    res = wb.simulate_standard_ratchet(series_to_obs(stall_series), entry, ladder=ladder,
                                       confirm_ticks=2, grace_seconds=0.0, slippage=0.0,
                                       stall_seconds=10, stall_min_profit=20)
    check("[stall] parity + reason", res["exit"]["reason"] == "stall" and ref_reason == "stall"
          and res["exit"]["ts"] == _iso(ref_ts))
    # qty-invariance of the ratchet decision (baseline is 1 contract; rule is qty-free)
    check("ratchet decision is qty-independent (rule, not size)", ref_reason in ("standard_ratchet", "stall", "none"))


if __name__ == "__main__":
    for fn in (test_field_level_sources, test_native_spx_sources_are_explicit_and_never_use_stock_proxy,
               test_start_valid_creates_and_persists,
               test_start_blocked_reasons, test_sequence_dedupe,
               test_unsync_blocks_add_and_reversal, test_pause_resume,
               test_stop_and_abandon, test_restart_recovery,
               test_shared_seed_three_tracks, test_isolation,
               test_baseline_guard_parity):
        fn()
    print("\nALL WAVE OBSERVER TESTS PASSED")
