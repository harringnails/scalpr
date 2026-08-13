"""
Cohort A reporting tests (wave-riding-v0-observation-cohort-a). REPORT-ONLY.

Drives the observer to produce completed shadow simulations, then checks the
per-tranche attribution and the 8-section cohort report. Behavior is unchanged;
this only reads artifacts. Isolated in a temp CWD.
"""
import os
import tempfile
from datetime import datetime, timezone

TMP = tempfile.mkdtemp()
os.chdir(TMP)

import wave_quotes as wq            # noqa: E402
import wave_store as wst            # noqa: E402
import wave_cohort as wcohort       # noqa: E402
from wave_config import WaveConfig  # noqa: E402
from wave_observer import ShadowObserver  # noqa: E402


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


def obs(seq, now, u, obid, oask, side="CALL", contract="SPY260101C00100000",
        underlying="SPY", cfg=None):
    cfg = cfg or CFG()
    uq = {"bid": u - 0.01, "ask": u + 0.01, "provider_ts_epoch": now, "provider_ts_iso": _iso(now)}
    oq = {"bid": obid, "ask": oask, "provider_ts_epoch": now, "provider_ts_iso": _iso(now)}
    atr = {"atr_value": 1.0, "atr_quality": "FULL", "atr_version": "intraday-atr-v0"}
    sess = {"market_open": True, "in_open_window": False, "in_close_window": False, "session_minute": 30}
    return wq.assemble_observation(sequence=seq, now_epoch=now, now_iso=_iso(now), side=side,
                                   underlying_symbol=underlying, contract_symbol=contract,
                                   u_quote=uq, o_quote=oq, atr_audit=atr, cfg=cfg, session=sess,
                                   vwap=u - 1.0, feed="iex")


def _run_call_with_add(pid, base_now, underlying="SPY", contract="SPY260101C00100000"):
    ob = ShadowObserver(CFG())
    ob.start(pid, underlying, contract, "CALL", obs(0, base_now, 100, 1.00, 1.02,
             underlying=underlying, contract=contract))
    pos = wst.load_position(pid)
    pos = ob.observe(pos, obs(1, base_now + 2, 100.6, 1.20, 1.22, underlying=underlying, contract=contract))["position"]
    pos = ob.observe(pos, obs(2, base_now + 13, 100.6, 1.20, 1.22, underlying=underlying, contract=contract))["position"]
    ob.stop(pos, obs(3, base_now + 20, 100.6, 1.30, 1.32, underlying=underlying, contract=contract))
    return ob


def _run_put_no_add(pid, base_now, underlying="QQQ", contract="QQQ260101P00400000"):
    ob = ShadowObserver(CFG())
    ob.start(pid, underlying, contract, "PUT", obs(0, base_now, 400, 2.00, 2.02,
             side="PUT", underlying=underlying, contract=contract))
    pos = wst.load_position(pid)
    ob.stop(pos, obs(1, base_now + 30, 400, 2.10, 2.12, side="PUT", underlying=underlying, contract=contract))
    return ob


def test_freeze_config():
    print("Cohort config is frozen + hashed; no thresholds change")
    rec = wcohort.freeze_cohort_config()
    check("cohort id set", rec["cohort_id"] == "wave-riding-v0-observation-cohort-a")
    check("config + hash present", rec["config_hash"] and "config" in rec)
    check("targets recorded", rec["target"]["min_simulations"] == 30)


def test_tranche_attribution():
    print("Per-tranche attribution: initial + addition, full field set, contributions sum ~1")
    _run_call_with_add("c1", 1000)
    r = wcohort.position_tranches("c1")
    check("two tranches (initial + addition_1)",
          [t["label"] for t in r["tranches"]] == ["initial", "addition_1"])
    t0, t1 = r["tranches"]
    for k in ("fill_ts", "fill_price", "quantity", "capital_committed_usd", "exit_timestamp",
              "exit_price", "gross_pnl_usd", "net_pnl_usd_est", "mfe_pct", "mae_pct",
              "time_held_seconds", "contribution_to_total_pnl_pct"):
        check(f"initial tranche has {k}", k in t0 and t0[k] is not None)
    check("initial fill = ask+slip (1.04)", abs(t0["fill_price"] - 1.04) < 1e-9)
    check("addition fill = ask+slip (1.24)", abs(t1["fill_price"] - 1.24) < 1e-9)
    check("gross P&L computed per tranche", t0["gross_pnl_usd"] > 0 and t1["gross_pnl_usd"] >= 0)
    csum = t0["contribution_to_total_pnl_pct"] + t1["contribution_to_total_pnl_pct"]
    check("contributions sum to ~1.0", abs(csum - 1.0) < 1e-6)
    check("addition improved/reduced flagged", isinstance(t1["addition_improved_result"], bool))
    check("incremental per added dollar present", r["incremental_pnl_per_added_dollar"] is not None)
    check("seconds_from_final_add_to_exit present", r["seconds_from_final_add_to_exit"] is not None)
    check("seconds_from_each_add_to_exit list", isinstance(r["seconds_from_each_add_to_exit"], list))


def test_cohort_report_sections_and_gate():
    print("8-section cohort report + acceptance gate (not met at small N); not an edge claim")
    _run_call_with_add("c2", 5000, underlying="AAPL", contract="AAPL260101C00200000")
    _run_put_no_add("p2", 9000)
    rep = wcohort.build_cohort_report(["c1", "c2", "p2"])
    for k in ("1_operational_integrity", "2_synchronization_and_quote_quality",
              "3_add_trigger_behavior", "4_reversal_behavior", "5_tranche_level_performance",
              "6_wave_riding_vs_standard_ratchet", "7_wave_riding_vs_one_contract_hold",
              "8_outcomes_by_dimension"):
        check(f"section present: {k}", k in rep)
    check("report-only + not edge claim", rep["report_only"] is True and rep["is_edge_claim"] is False)
    a = rep["1_operational_integrity"]
    check("acceptance gate computed", a["completed_simulations"] >= 2 and a["cohort_complete"] is False)
    check("direction split tracked", "CALL" in rep["8_outcomes_by_dimension"]["direction"]
          and "PUT" in rep["8_outcomes_by_dimension"]["direction"])
    check("delta reported as unavailable (honest)", rep["8_outcomes_by_dimension"]["delta"] == "unavailable_in_observer_v0")
    check("baselines compared", rep["6_wave_riding_vs_standard_ratchet"]["compared"] >= 1)
    md = wcohort.render_markdown(rep)
    check("markdown renders with title", "Operational Cohort" in md)


if __name__ == "__main__":
    for fn in (test_freeze_config, test_tranche_attribution, test_cohort_report_sections_and_gate):
        fn()
    print("\nALL WAVE COHORT TESTS PASSED")
