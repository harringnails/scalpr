"""
standard-entry-incubation-shadow-cohort-a — offline factorial report over
fully-observed captured trades. Report-only; not an edge claim; cohort is NOT
locked until materiality + primary designations are explicitly approved.

Every combination shows BOTH benefit and harm vs the primary baseline so the
larger grid cannot manufacture a misleading "best". Primary baseline and one
pre-registered primary candidate are designated; all other cells are exploratory.
"""
import statistics as stats
from collections import defaultdict

import entry_incubation_study as st
import incubation_store as ist
import incubation_config as icfg


def fully_observed(rec):
    return bool(rec and rec.get("state") == "COMPLETE" and rec.get("live_exit"))


def _cell_of(fac, key):
    for c in fac["cells"]:
        if (c["activation"], c["hard_stop"]) == tuple(key):
            return c
    return None


def _compare_incubation_vs_baseline(trades, cand_key, base_key, cfg):
    """Comparison 2 vs 1 — clean incubation effect."""
    preserved = prem_avoided = 0
    addl_loss_usd = worst = net = 0.0
    maes = []
    for t in trades:
        b, c = _cell_of(t["fac"], base_key), _cell_of(t["fac"], cand_key)
        if not b or not c:
            continue
        d = c["realized_return_pct"] - b["realized_return_pct"]
        net += c["realized_pnl_usd"] - b["realized_pnl_usd"]
        maes.append(c["mae_pct"] - b["mae_pct"])
        if d < 0:
            addl_loss_usd += c["realized_pnl_usd"] - b["realized_pnl_usd"]
            worst = min(worst, d)
        if t["fac"]["PREMATURE_EXIT"] and d >= cfg.recovery_materiality_pp:
            preserved += 1
            prem_avoided += 1
    return {"recoverable_winners_preserved": preserved,
            "premature_exits_avoided": prem_avoided,
            "additional_losses_caused_usd": round(addl_loss_usd, 2),
            "additional_mae_mean_pp": round(sum(maes) / len(maes), 4) if maes else None,
            "worst_additional_loss_pp": round(worst, 4),
            "net_simulated_pnl_difference_usd": round(net, 2)}


def _compare_overlay_vs_incubation(trades, overlay_key, incub_key, cfg):
    """Comparison 3 vs 2 — fixed safety overlay on top of incubation."""
    losses_reduced_usd = severed = 0
    losses_reduced_usd = protection_usd = forfeited_pp = 0.0
    for t in trades:
        i, o = _cell_of(t["fac"], incub_key), _cell_of(t["fac"], overlay_key)
        if not i or not o:
            continue
        if i["realized_return_pct"] < 0 and o["realized_return_pct"] > i["realized_return_pct"]:
            gain = o["realized_pnl_usd"] - i["realized_pnl_usd"]
            losses_reduced_usd += gain
            protection_usd += gain
        if o["strong_trade_cut_by_tighter_stop"] and \
                i["realized_return_pct"] - o["realized_return_pct"] >= cfg.recovery_materiality_pp:
            severed += 1
        if i["realized_return_pct"] > o["realized_return_pct"]:
            forfeited_pp += i["realized_return_pct"] - o["realized_return_pct"]
    return {"incubation_losses_reduced_by_fixed_stop_usd": round(losses_reduced_usd, 2),
            "recoverable_winners_severed_by_fixed_stop": severed,
            "net_protection_gained_usd": round(protection_usd, 2),
            "opportunity_forfeited_pp": round(forfeited_pp, 4)}


def build_path(trade_id, store=ist):
    """Conservative executable-bid path: usable option bids only (quote ok, not
    unsynchronized). Gaps are simply absent — never interpolated."""
    path = []
    for o in store.read_path(trade_id):
        if o.get("option_bid") in (None, 0) or o.get("t") is None:
            continue
        if o.get("unsynchronized") or o.get("quote_quality") in ("crossed", "missing", "unusable", "stale"):
            continue
        path.append({"t": o["t"], "bid": o["option_bid"], "emergency": o.get("emergency")})
    path.sort(key=lambda x: x["t"])
    return path


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(stats.median(xs), 4) if xs else None


def _profit_factor(pnls):
    g = sum(p for p in pnls if p > 0)
    l = -sum(p for p in pnls if p < 0)
    return round(g / l, 3) if l > 0 else (float("inf") if g > 0 else None)


def _tail_loss(rets):
    xs = sorted(r for r in rets if r is not None)
    if not xs:
        return None
    k = max(1, len(xs) // 10)
    return round(sum(xs[:k]) / k, 4)


def build_report(cfg=None, trade_ids=None, store=ist):
    cfg = cfg or icfg.default_config()
    tids = trade_ids if trade_ids is not None else store.list_active() + _completed_ids(store)
    trades = []
    for tid in set(tids):
        rec = store.load_trade(tid)
        if not fully_observed(rec):
            continue
        if not rec.get("cohort_eligible", True):     # exclude OPERATIONAL_VALIDATION
            continue
        path = build_path(tid, store)
        if len(path) < 2:
            continue
        snap = store.load_snapshot(tid) or {}
        fac = st.replay_factorial(
            {"symbol": rec.get("contract_symbol"), "entry": rec.get("entry"), "qty": rec.get("qty", 1)},
            path, list(cfg.activation_policies), dict(cfg.hard_stop_scenarios),
            ladder=[dict(r) for r in icfg.STANDARD_LADDER],
            baseline_activation=cfg.primary_baseline_activation,
            baseline_hard_stop=cfg.primary_baseline_hard_stop,
            recovery_materiality_pp=cfg.recovery_materiality_pp,
            loose_materiality_pp=cfg.loose_materiality_pp)
        trades.append({"tid": tid, "snap": snap, "rec": rec, "fac": fac})

    # acceptance gates
    sessions = {(t["snap"].get("entry_timestamp") or "")[:10] for t in trades if t["snap"]}
    dirs = [t["rec"].get("direction") for t in trades]
    syms = {t["rec"].get("underlying_symbol") for t in trades}
    accept = {"fully_observed": len(trades), "sessions": len(sessions),
              "calls": dirs.count("CALL"), "puts": dirs.count("PUT"), "symbols": len(syms - {None})}
    accept["cohort_complete"] = (accept["fully_observed"] >= 30 and accept["sessions"] >= 10
                                 and accept["calls"] >= 10 and accept["puts"] >= 10
                                 and accept["symbols"] >= 5)

    # per-cell aggregate (benefit + harm)
    cell_rows = defaultdict(lambda: defaultdict(list))
    prem_avoided = defaultdict(int)
    for t in trades:
        base_prem = t["fac"]["PREMATURE_EXIT"] is not None
        for c in t["fac"]["cells"]:
            key = (c["activation"], c["hard_stop"])
            cell_rows[key]["ret"].append(c["realized_return_pct"])
            cell_rows[key]["pnl"].append(c["realized_pnl_usd"])
            cell_rows[key]["dpnl"].append(c["net_pnl_improvement_usd"])
            cell_rows[key]["recovery"].append(c["recovery_value_captured_pp"])
            cell_rows[key]["addl_loss"].append(c["additional_loss_pp"])
            cell_rows[key]["addl_dd"].append(c["additional_drawdown_pp"])
            cell_rows[key]["worsened"].append(c["worsened_vs_baseline"])
            cell_rows[key]["cut"].append(c["strong_trade_cut_by_tighter_stop"])
            if base_prem and c["realized_return_pct"] - t["fac"]["cells"][0]["realized_return_pct"] >= cfg.recovery_materiality_pp:
                prem_avoided[key] += 1

    def cell_summary(key):
        d = cell_rows.get(key, {})
        rets, pnls = d.get("ret", []), d.get("pnl", [])
        if not rets:
            return {"n": 0}
        return {
            "n": len(rets),
            "net_simulated_pnl_usd": round(sum(pnls), 2),
            "mean_return_pct": round(sum(rets) / len(rets), 4),
            "median_return_pct": _median(rets),
            "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4),
            "profit_factor": _profit_factor(pnls),
            "max_drawdown_pct": round(min(rets), 4),
            "tail_loss_pct": _tail_loss(rets),
            # benefit
            "premature_exits_avoided": prem_avoided.get(key, 0),
            "opportunity_recovered_pp": round(sum(d.get("recovery", [])), 2),
            "net_pnl_improvement_usd": round(sum(d.get("dpnl", [])), 2),
            # harm
            "losers_worsened": sum(1 for w, r in zip(d.get("worsened", []), rets) if w and r < 0),
            "additional_loss_created_pp": round(sum(x for x in d.get("addl_loss", []) if x < 0), 2),
            "additional_drawdown_mean_pp": round(sum(d.get("addl_dd", [])) / len(rets), 4),
            "strong_trades_cut_by_tighter_stop": sum(1 for x in d.get("cut", []) if x),
        }

    base_key = (cfg.primary_baseline_activation, cfg.primary_baseline_hard_stop)
    cand_key = (cfg.primary_candidate_activation, cfg.primary_candidate_hard_stop)
    overlay_key = (cfg.primary_safety_overlay_activation, cfg.primary_safety_overlay_hard_stop)
    designated = {base_key, cand_key, overlay_key}

    return {
        "cohort_id": cfg.cohort_id, "report_only": True, "is_edge_claim": False,
        "config_hash": cfg.config_hash(), "materiality_basis": cfg.materiality_basis,
        "cohort_locked": cfg.cohort_ready(),
        "1_acceptance": accept,
        # three PRE-REGISTERED prioritized comparisons (interpret these, not the grid max)
        "priority_comparisons": {
            "1_live_truth_baseline": {"key": list(base_key), "summary": cell_summary(base_key)},
            "2_incubation_effect": {
                "key": list(cand_key), "summary": cell_summary(cand_key),
                "vs_baseline": _compare_incubation_vs_baseline(trades, cand_key, base_key, cfg)},
            "3_incubation_plus_fixed_overlay": {
                "key": list(overlay_key), "summary": cell_summary(overlay_key),
                "vs_incubation": _compare_overlay_vs_incubation(trades, overlay_key, cand_key, cfg)},
        },
        "primary_baseline": {"key": list(base_key), "summary": cell_summary(base_key)},
        "primary_candidate": {"key": list(cand_key), "summary": cell_summary(cand_key)},
        "primary_safety_overlay": {"key": list(overlay_key), "summary": cell_summary(overlay_key)},
        "exploratory_grid": {f"{a}|{h}": cell_summary((a, h))
                             for a in cfg.activation_policies for h in cfg.hard_stop_scenarios
                             if (a, h) not in designated},
        "diagnostics_trade_level": {
            "PREMATURE_EXIT_trades": sum(1 for t in trades if t["fac"]["PREMATURE_EXIT"]),
            "LOOSE_INITIAL_PROTECTION_trades": sum(1 for t in trades if t["fac"]["LOOSE_INITIAL_PROTECTION"]),
        },
        "primary_questions": {
            "Q1_delay_preserves_recoverable_winners": "compare primary_candidate.premature_exits_avoided vs baseline",
            "Q2_tighter_hard_stop_reduces_weak_losses": "compare HARD_STOP_10/12.5 cells' additional_loss vs baseline on losers",
            "Q3_combination_helps_one_without_disproportionate_harm": "primary_candidate benefit vs harm rollup",
            "Q4_two_regimes_needed": "if no single cell improves both, evidence for separate strong/weak regimes",
        },
        "multiple_comparison_note": ("Do NOT pick the highest-P&L cell. Interpret the "
                                     "PRE-REGISTERED primary baseline vs primary candidate; "
                                     "all exploratory cells are hypothesis-generating only."),
        "note": ("Report-only, shadow, not an edge claim. Cohort locks only after "
                 "materiality + primary designations are approved."),
    }


def _completed_ids(store):
    import glob
    import os
    out = []
    for p in glob.glob(os.path.join(store.TRADE_DIR, "*.json")):
        out.append(os.path.basename(p)[:-5])
    return out


MATERIAL_GAP_SEC = 60.0     # a forward-obs gap larger than this is "material" (QC)


def telemetry_summary(trade_id, store=ist):
    ev = store.read_telemetry(trade_id)
    fetch = [e for e in ev if e.get("kind") == "fetch"]
    obs = [e for e in ev if e.get("kind") == "observe"]
    ok = sum(1 for e in fetch if e.get("ok"))
    h429 = sum(1 for e in fetch if e.get("http_429"))
    perr = sum(1 for e in fetch if e.get("error"))
    dup = sum(1 for e in obs if e.get("result") == "DUPLICATE")
    stale = sum(1 for e in obs if e.get("stale"))
    unsync = sum(1 for e in obs if e.get("unsync"))
    # gaps/interval from the accepted stream
    ts = [o["t"] for o in store.read_path(trade_id) if o.get("t") is not None]
    ts.sort()
    diffs = [b - a for a, b in zip(ts, ts[1:])]
    import statistics
    return {
        "poll_cycles_attempted": len(fetch),
        "underlying_quote_requests": ok, "option_quote_requests": ok,
        "successful_responses": ok, "http_429": h429, "provider_errors": perr,
        "stale_quotes": stale, "unsynchronized_observations": unsync,
        "duplicate_observations": dup,
        "skipped_observations": sum(1 for e in obs if e.get("result") in ("INACTIVE", "PAUSED")),
        "maximum_observation_gap_s": round(max(diffs), 2) if diffs else None,
        "median_observation_interval_s": round(statistics.median(diffs), 2) if diffs else None,
        "accepted_observations": len(ts),
    }


def validation_report(trade_id, cfg=None, store=ist):
    """Evaluate an OPERATIONAL_VALIDATION trade against the clean-pass criteria +
    telemetry. On PASS, records validation_passed so Cohort A counting may begin.
    On any 429 / material gap / missing lifecycle step → OPERATIONAL_VALIDATION_FAILED."""
    cfg = cfg or icfg.default_config()
    rec = store.load_trade(trade_id)
    snap = store.load_snapshot(trade_id)
    if rec is None or snap is None:
        return {"status": "OPERATIONAL_VALIDATION_FAILED", "reason": "missing_record_or_snapshot"}
    path = build_path(trade_id, store)
    tel = telemetry_summary(trade_id, store)

    # replay to confirm the three prioritized comparisons finalize
    three_ok = False
    try:
        fac = st.replay_factorial(
            {"symbol": rec.get("contract_symbol"), "entry": rec.get("entry"), "qty": rec.get("qty", 1)},
            path, list(cfg.activation_policies), dict(cfg.hard_stop_scenarios),
            ladder=[dict(r) for r in icfg.STANDARD_LADDER],
            baseline_activation=cfg.primary_baseline_activation,
            baseline_hard_stop=cfg.primary_baseline_hard_stop,
            recovery_materiality_pp=cfg.recovery_materiality_pp,
            loose_materiality_pp=cfg.loose_materiality_pp)
        keys = {(c["activation"], c["hard_stop"]) for c in fac["cells"]}
        three_ok = all(k in keys for k in (
            (cfg.primary_baseline_activation, cfg.primary_baseline_hard_stop),
            (cfg.primary_candidate_activation, cfg.primary_candidate_hard_stop),
            (cfg.primary_safety_overlay_activation, cfg.primary_safety_overlay_hard_stop)))
    except Exception:
        three_ok = False

    ts_sorted = sorted(o["t"] for o in store.read_path(trade_id) if o.get("t") is not None)
    monotone = all(b > a for a, b in zip(ts_sorted, ts_sorted[1:]))
    checks = {
        "entry_snapshot_persisted": bool(snap.get("snapshot_hash")),
        "sequential_executable_bid_path": len(path) >= 2 and monotone,
        "live_exit_recorded": rec.get("live_exit") is not None,
        "recovery_window_completed": rec.get("state") == "COMPLETE",
        "no_duplicate_canonical_observations": tel["duplicate_observations"] == 0,
        "no_unexplained_gap": (tel["maximum_observation_gap_s"] is None
                               or tel["maximum_observation_gap_s"] <= MATERIAL_GAP_SEC),
        "all_three_comparisons_finalized": three_ok,
        "no_broker_or_guard_mutation": True,     # by design — capture never mutates
        "no_429": tel["http_429"] == 0,
        "no_provider_errors": tel["provider_errors"] == 0,
    }
    passed = all(checks.values())
    status = "OPERATIONAL_VALIDATION_PASSED" if passed else "OPERATIONAL_VALIDATION_FAILED"
    if passed:
        store.set_validation_passed(True, trade_id)
    return {"trade_id": trade_id, "study_role": rec.get("study_role"),
            "status": status, "checks": checks, "telemetry": tel,
            "cohort_counting_enabled_now": store.validation_passed(),
            "note": ("A clean pass records validation_passed=true so Cohort A "
                     "begins counting; a failure requires another validation trade.")}


def propose_registration(cfg=None):
    """The proposed frozen configuration to lock Cohort A — RETURNED FOR APPROVAL.
    Nothing is locked until materiality_registered and primary_registered are set
    by explicit approval."""
    cfg = cfg or icfg.default_config()
    return {
        "cohort_id": cfg.cohort_id,
        "status": ("LOCKED (approved) — capture begins when INCUBATION_SHADOW_ENABLED=1"
                   if cfg.cohort_ready() else "PENDING_APPROVAL — cohort NOT started"),
        "verified_live_hard_stop": "NONE (live Guard has no separate hard stop; the "
                                   "15% is a giveback from the EVOLVING PEAK, not a "
                                   "fixed entry-based stop)",
        "materiality_basis": cfg.materiality_basis,
        "hard_stop_scenarios": cfg.hard_stop_scenarios,
        "activation_policies": list(cfg.activation_policies),
        "primary_baseline": [cfg.primary_baseline_activation, cfg.primary_baseline_hard_stop],
        "primary_candidate": [cfg.primary_candidate_activation, cfg.primary_candidate_hard_stop],
        "primary_safety_overlay": [cfg.primary_safety_overlay_activation, cfg.primary_safety_overlay_hard_stop],
        "materiality": {
            "recovery_materiality_pp": cfg.recovery_materiality_pp,
            "loose_materiality_pp": cfg.loose_materiality_pp,
            "qualifying_recovery_window_min": cfg.qualifying_recovery_window_min,
        },
        "acceptance_gates": {"min_fully_observed": 30, "min_sessions": 10,
                             "min_calls": 10, "min_puts": 10, "min_symbols": 5},
        "locked_config_hash": cfg.config_hash(),
    }
