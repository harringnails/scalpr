"""
Tests for entry_incubation_study.py (standard-entry-incubation-study-v0).
Validates the replay engine on SYNTHETIC option paths (real logs lack option
tick paths — see data_coverage). No live Guard is touched.
"""
import entry_incubation_study as st


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok: {name}")


def P(pairs):
    return [{"t": t, "bid": b} for (t, b) in pairs]


# ── winner: early dip stops CURRENT out, then a big recovery ────────────────
def test_winner_recovery():
    print("Early-dip-then-recovery: CURRENT exits prematurely; delay/buffer capture it")
    entry = 1.00
    path = P([(0,1.00),(30,1.00),(55,1.00),(70,0.84),(80,0.83),
              (120,1.05),(180,1.20),(300,1.50),(360,1.55),(600,1.50),(660,1.48)])
    r = st.replay_trade({"symbol":"X","entry":entry,"qty":1}, path)
    cur = r["variants"]["CURRENT"]
    d5  = r["variants"]["TIME_DELAY_5M"]
    tp  = r["variants"]["TIME_OR_PROFIT"]
    check("CURRENT stopped out at a loss", cur["realized_return_pct"] < 0)
    check("TIME_DELAY_5M captured the recovery", d5["realized_return_pct"] > 30)
    check("TIME_OR_PROFIT captured the recovery", tp["realized_return_pct"] > 30)
    check("PREMATURE_EXIT flagged", r["PREMATURE_EXIT"] is not None
          and r["PREMATURE_EXIT"]["opportunity_missed_pp"] > 30)
    check("delay beats current materially",
          d5["realized_return_pct"] - cur["realized_return_pct"] > 50)


# ── loser: monotonic decline — delay HARMS, but hard stop caps it ───────────
def test_loser_harm_capped_by_hard_stop():
    print("Monotonic loser: delayed variants enlarge the loss, but the hard stop caps it")
    entry = 1.00
    path = P([(0,1.00),(60,0.98),(120,0.95),(180,0.90),(240,0.86),
              (300,0.82),(360,0.78),(420,0.74),(480,0.72),(540,0.70)])
    r = st.replay_trade({"symbol":"L","entry":entry,"qty":1}, path)
    cur = r["variants"]["CURRENT"]["realized_return_pct"]
    d5  = r["variants"]["TIME_DELAY_5M"]["realized_return_pct"]
    pb  = r["variants"]["PROFIT_BUFFER_10"]["realized_return_pct"]
    check("CURRENT exits on the initial rung", cur < 0)
    check("delay enlarges the loss vs CURRENT", d5 <= cur)
    check("profit-buffer never arms -> rides to hard stop", pb <= cur)
    check("hard stop caps the downside near -25%",
          st.replay_trade({"symbol":"L","entry":entry,"qty":1}, path)["variants"]["PROFIT_BUFFER_10"]["realized_return_pct"] >= -30)
    check("hard-stop exit reason present",
          "hard_stop" in r["variants"]["PROFIT_BUFFER_10"]["exit_reason"])


# ── fast crater: hard stop fires during incubation ──────────────────────────
def test_hard_stop_during_incubation():
    print("Fast crater during incubation triggers the hard stop (not left unprotected)")
    entry = 1.00
    path = P([(0,1.00),(30,0.90),(60,0.80),(90,0.72),(120,0.68)])  # -32% by 2 min
    r = st.replay_variant("TIME_DELAY_5M", entry, path)
    check("hard stop fired", "hard_stop" in r["exit_reason"])
    check("loss capped near -25%", r["realized_return_pct"] >= -30)


# ── activation triggers behave ──────────────────────────────────────────────
def test_activation_triggers():
    print("Activation triggers: time, profit buffer, time-or-profit, never-armed")
    entry = 1.00
    # reaches +6% at t=120, +10% at t=200
    path = P([(0,1.00),(60,1.03),(120,1.06),(200,1.10),(400,1.12),(700,1.05)])
    check("PROFIT_BUFFER_5 arms at +5%",
          st.replay_variant("PROFIT_BUFFER_5", entry, path)["activation_timestamp_s"] == 120)
    check("PROFIT_BUFFER_10 arms at +10%",
          st.replay_variant("PROFIT_BUFFER_10", entry, path)["activation_timestamp_s"] == 200)
    check("TIME_OR_PROFIT arms at +10% before 5m",
          st.replay_variant("TIME_OR_PROFIT", entry, path)["activation_timestamp_s"] == 200)
    check("TIME_DELAY_5M arms at 5 min",
          st.replay_variant("TIME_DELAY_5M", entry, path)["activation_timestamp_s"] == 400)
    # a trade that never profits -> PROFIT_BUFFER never arms
    flat = P([(0,1.00),(120,0.99),(300,0.98),(600,0.98)])
    nb = st.replay_variant("PROFIT_BUFFER_10", entry, flat)
    check("profit buffer never arms on a non-profitable trade",
          nb["activation_reason"] == "trigger_never_met")


# ── real-data coverage: the honest gate ─────────────────────────────────────
def test_data_coverage_is_honest():
    print("Coverage over existing logs: option tick paths are absent -> not replayable")
    cov = st.data_coverage()
    check("journal has trades", cov["journal_trades"] > 0)
    check("tick log is underlying-only (no option contracts)",
          cov["tick_log_symbols"] == ["SPY"] and not cov["option_contracts_with_tick_history"])
    check("verdict says NOT REPLAYABLE", "NOT REPLAYABLE" in cov["verdict"])
    ep = st.endpoint_only_diagnostics()
    check("endpoint-only LOOSE_INITIAL_PROTECTION count computed",
          ep["LOOSE_INITIAL_PROTECTION_candidates"] >= 0 and ep["trades_scored"] > 0)


if __name__ == "__main__":
    for fn in (test_winner_recovery, test_loser_harm_capped_by_hard_stop,
               test_hard_stop_during_incubation, test_activation_triggers,
               test_data_coverage_is_honest):
        fn()
    print("\nALL ENTRY INCUBATION TESTS PASSED")
