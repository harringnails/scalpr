"""
ENTRY_INCUBATION_SHADOW tests — capture lifecycle + factorial cohort + isolation.
Isolated in a temp CWD. No live Guard, no broker, nothing live.
"""
import inspect
import tempfile

import pytest

TMP = tempfile.mkdtemp()

import entry_incubation_study as st        # noqa: E402
import incubation_config as icfg           # noqa: E402
import incubation_shadow as ish            # noqa: E402
import incubation_cohort as icoh           # noqa: E402
import incubation_store as ist             # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cwd(monkeypatch):
    monkeypatch.chdir(TMP)


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok: {name}")


def obs(t, bid, qts, **kw):
    o = {"t": t, "ts": f"2026-07-30T15:{t//60:02d}:{t%60:02d}+00:00",
         "option_bid": bid, "option_ask": bid + 0.02, "option_mid": bid + 0.01,
         "option_quote_timestamp": qts, "underlying_price": 100.0,
         "underlying_quote_timestamp": qts, "quote_age_sec": 0, "spread_pct": 2.0,
         "timestamp_skew_seconds": 0.0, "quote_quality": "ok", "unsynchronized": False,
         "atr_value": 1.0, "atr_quality": "FULL", "above_vwap_side_ok": True,
         "sources": {"option_quote": "alpaca_options_latest_quote:opra",
                     "underlying_quote": "alpaca_stock_latest_quote:iex"}}
    o.update(kw)
    return o


# ── verified live hard stop = NONE ──────────────────────────────────────────
def test_verified_hard_stop_none():
    print("CURRENT_CONFIGURED_HARD_STOP registered as None (verified from code)")
    check("scenario present + None", icfg.HARD_STOP_SCENARIOS["CURRENT_CONFIGURED_HARD_STOP"] is None)
    check("four scenarios", len(icfg.HARD_STOP_SCENARIOS) == 4)
    prop = icoh.propose_registration()
    check("proposal states verified NONE", "NONE" in prop["verified_live_hard_stop"])
    check("cohort LOCKED (approved)", prop["status"].startswith("LOCKED"))
    check("config ready (approved)", icfg.default_config().cohort_ready() is True)
    check("primary candidate = TIME_OR_PROFIT x None",
          prop["primary_candidate"] == ["TIME_OR_PROFIT", "CURRENT_CONFIGURED_HARD_STOP"])
    check("safety overlay = TIME_OR_PROFIT x HARD_STOP_15",
          prop["primary_safety_overlay"] == ["TIME_OR_PROFIT", "HARD_STOP_15"])
    check("materiality basis = executable_option_bid", prop["materiality_basis"] == "executable_option_bid")


# ── factorial: tighter stop severs the early-dip winner; no-stop captures it ─
def test_factorial_two_problem_tension():
    print("Factorial exposes the tension: tight stop cuts strong trade, baseline premature")
    cfg = icfg.default_config()
    path = [{"t": t, "bid": b} for (t, b) in
            [(0,1.0),(30,1.0),(70,0.84),(80,0.83),(180,1.20),(360,1.55),(600,1.48)]]
    fac = st.replay_factorial({"symbol":"X","entry":1.0,"qty":1}, path,
                              list(cfg.activation_policies), dict(cfg.hard_stop_scenarios),
                              ladder=icfg.STANDARD_LADDER,
                              recovery_materiality_pp=cfg.recovery_materiality_pp,
                              loose_materiality_pp=cfg.loose_materiality_pp)
    cells = {(c["activation"], c["hard_stop"]): c for c in fac["cells"]}
    check("32 cells", len(cells) == 32)
    check("baseline (CURRENT+CONFIGURED) exits premature at a loss",
          cells[("CURRENT","CURRENT_CONFIGURED_HARD_STOP")]["realized_return_pct"] < 0)
    check("PREMATURE_EXIT flagged", fac["PREMATURE_EXIT"] is not None)
    tight = cells[("TIME_OR_PROFIT","HARD_STOP_10")]
    loose = cells[("TIME_OR_PROFIT","CURRENT_CONFIGURED_HARD_STOP")]
    check("tight hard stop cuts the strong trade", tight["strong_trade_cut_by_tighter_stop"] is True)
    check("no-stop incubation captures the recovery", loose["realized_return_pct"] > 30)


# ── capture lifecycle: snapshot immutable, dedupe, state machine ────────────
def test_capture_lifecycle():
    print("Capture: entry snapshot immutable, duplicate quote deduped, states advance")
    cfg = icfg.IncubationConfig(recovery_window_minutes=1)
    gf = {"entry":1.00,"qty":1,"direction":"CALL","underlying_symbol":"SPY",
          "contract_symbol":"SPY260730C00737000","entry_timestamp":"2026-07-30T15:00:00+00:00",
          "delta":0.5,"gamma":0.02,"theta":-0.01,"iv":0.3,"dte":0,"strike":737.0,
          "expiration":"2026-07-30","rules_version":"whole-position-ratchet",
          "guard_config_version":"ladder-15-6-2.5"}
    r = ish.on_entry("T1","P1", gf, obs(0,1.00,"q0"), cfg)
    check("created with snapshot hash", r["created"] and r["snapshot_hash"])
    check("snapshot immutable (second save no-op)", ist.save_snapshot(ist.load_snapshot("T1")) is None)
    # duplicate quote timestamp -> no-op
    ish.observe("T1", obs(30,1.10,"q1"), cfg)
    dup = ish.observe("T1", obs(31,1.11,"q1"), cfg)   # same option_quote_timestamp q1
    check("duplicate quote deduped", dup["code"] == "DUPLICATE_OBSERVATION")
    # live exit -> recovery
    ish.observe("T1", obs(60,1.05,"q2"), cfg, live_guard={"state":"done","peak":10,"exit_event":"dip tol 6%"})
    rec = ist.load_trade("T1")
    check("entered recovery after live exit", rec["state"] == "OBSERVING_RECOVERY")
    # past recovery window -> COMPLETE
    ish.observe("T1", obs(130,1.02,"q3"), cfg)
    rec = ist.load_trade("T1")
    check("recovery window complete -> COMPLETE", rec["state"] == "COMPLETE")
    check("unregistered from active", "T1" not in ist.list_active())


# ── operational-validation role: first trade excluded, clean pass unlocks cohort
def _capture_clean(tid, cfg, start_underlying="SPY"):
    gf = {"entry":1.00,"qty":1,"direction":"CALL","underlying_symbol":"SPY",
          "contract_symbol":"SPY260807C00600000","entry_timestamp":"2026-07-30T15:00:00+00:00",
          "delta":0.5,"gamma":0.02,"theta":-0.01,"iv":0.3,"dte":8,"strike":600.0,
          "expiration":"2026-08-07","rules_version":"whole-position-ratchet",
          "guard_config_version":"ladder-15-6-2.5"}
    ish.on_entry(tid,"P", gf, obs(0,1.00,tid+"q0"), cfg)
    ish.observe(tid, obs(30,1.10,tid+"q1"), cfg)                       # gap 30
    ish.observe(tid, obs(60,1.05,tid+"q2"), cfg, live_guard={"state":"done","peak":10,"exit_event":"dip"})
    ish.observe(tid, obs(90,1.03,tid+"q3"), cfg)                       # recovery
    ish.observe(tid, obs(120,1.02,tid+"q4"), cfg)                      # >= recovery_until -> COMPLETE


def test_validation_role_excluded_then_passes():
    print("First trade = OPERATIONAL_VALIDATION (excluded); a clean run unlocks Cohort A")
    ist.set_validation_passed(False)
    cfg = icfg.IncubationConfig(recovery_window_minutes=1)
    _capture_clean("VAL1", cfg)
    rec = ist.load_trade("VAL1")
    check("tagged OPERATIONAL_VALIDATION", rec["study_role"] == "OPERATIONAL_VALIDATION")
    check("cohort_eligible False", rec["cohort_eligible"] is False)
    check("exclusion reason set", rec["exclusion_reason"] == "INITIAL_PIPELINE_VALIDATION")
    rep = icoh.build_report(cfg)
    check("validation trade excluded from cohort acceptance", rep["1_acceptance"]["fully_observed"] == 0)
    vr = icoh.validation_report("VAL1", cfg)
    check("clean validation PASSED", vr["status"] == "OPERATIONAL_VALIDATION_PASSED")
    check("all clean-pass checks true", all(vr["checks"].values()))
    check("telemetry captured", "median_observation_interval_s" in vr["telemetry"])
    check("cohort counting now enabled", ist.validation_passed() is True)


def test_cohort_counts_after_validation():
    print("After validation passes, a new trade is COHORT-eligible and counts")
    ist.set_validation_passed(True)
    cfg = icfg.IncubationConfig(recovery_window_minutes=1)
    _capture_clean("COH1", cfg)
    rec = ist.load_trade("COH1")
    check("tagged COHORT", rec["study_role"] == "COHORT" and rec["cohort_eligible"] is True)
    rep = icoh.build_report(cfg)
    check("cohort trade counted", rep["1_acceptance"]["fully_observed"] >= 1)
    check("three priority comparisons present",
          set(rep["priority_comparisons"]) == {"1_live_truth_baseline", "2_incubation_effect",
                                               "3_incubation_plus_fixed_overlay"})
    check("report-only + LOCKED", rep["report_only"] and rep["cohort_locked"] is True)


def test_schema_hash_changed():
    print("Adding study-role fields bumped the record schema → new locked hash")
    dims = icfg.default_config().frozen_dimensions()
    check("record schema version in frozen dims",
          dims.get("record_schema_version") == "incubation-record-v0.1-study-role")


# ── isolation ───────────────────────────────────────────────────────────────
def test_isolation():
    print("No scalp_server import, no broker path in incubation modules")
    for m in (ish, ist, icoh, icfg, st):
        src = inspect.getsource(m)
        check(f"{m.__name__}: no scalp_server import", "import scalp_server" not in src)
        check(f"{m.__name__}: no submit_order",
              "submit_order" not in src and "MarketOrderRequest" not in src)


if __name__ == "__main__":
    for fn in (test_verified_hard_stop_none, test_factorial_two_problem_tension,
               test_capture_lifecycle, test_validation_role_excluded_then_passes,
               test_cohort_counts_after_validation, test_schema_hash_changed, test_isolation):
        fn()
    print("\nALL INCUBATION SHADOW TESTS PASSED")
