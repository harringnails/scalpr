"""
wave_cohort.py — Operational Validation Cohort A (`wave-riding-v0-observation-cohort-a`).

REPORTING ONLY. This module reads the stored shadow-simulation artifacts (seed,
orders, exits, observation stream, position record) and produces per-tranche
attribution + an 8-section cohort report. It NEVER changes add/exit behavior, and
no strategy threshold moves during the cohort — the config is frozen and hashed.

Purpose (explicit): identify operational defects, harmful add behavior, and
whether a larger formal study is justified. This is NOT a proof of trading edge.
No paper/live orders, no automated contract selection, no ML, no optimization.
"""
import re
from datetime import datetime, timezone

import feature_engine as fe
import wave_config as wc
import wave_store as wst
import wave_baselines as wb

COHORT_ID = "wave-riding-v0-observation-cohort-a"
COHORT_CONFIG_PATH = "wave_cohort_a_config.json"
COHORT_TARGET = {"min_simulations": 30, "min_sessions": 10, "min_calls": 10,
                 "min_puts": 10, "min_symbols": 5}
FEE_PER_CONTRACT = 0.65          # placeholder round-trip fee per contract (per side)

_EXIT_TERMINALS = ("WAVE_RIDING_REVERSAL", "HARD_MAX_LOSS_STOP", "EMERGENCY_KILL_SWITCH",
                   "MANUAL_SHADOW_STOP", "TERMINAL", "MARKET_DATA_SAFETY_EXIT")


# ── frozen cohort config ────────────────────────────────────────────────────

def freeze_cohort_config(path=COHORT_CONFIG_PATH):
    """Pin the CURRENT frozen Wave Riding config to the cohort id + hash. No
    thresholds change; this only records what the cohort is running under."""
    cfg = wc.default_config().to_dict()
    rec = {"cohort_id": COHORT_ID, "config": cfg,
           "config_hash": fe.canonical_hash(cfg),
           "frozen_at": datetime.now(timezone.utc).isoformat(),
           "target": COHORT_TARGET,
           "note": "thresholds frozen for the cohort; report-only; not an edge claim"}
    wst._atomic_write_json(path, rec)
    return rec


# ── helpers ─────────────────────────────────────────────────────────────────

def _epoch(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_occ(sym):
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", str(sym or ""))
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    try:
        exp = datetime(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]), tzinfo=timezone.utc).date()
    except Exception:
        return None
    return {"root": root, "expiry": exp, "type": cp, "strike": int(strike) / 1000.0}


def _orders(pid, path=None):
    p = path or wst.ORDERS_LOG
    return [r for r in fe._iter_jsonl(p)
            if r.get("position_id") == pid and r.get("action") == "ADD" and r.get("filled")]


def _exit(pid, path=None):
    p = path or wst.EXITS_LOG
    rows = [r for r in fe._iter_jsonl(p) if r.get("position_id") == pid and r.get("filled")]
    return rows[-1] if rows else None


# ── per-tranche attribution ─────────────────────────────────────────────────

def position_tranches(pid, cfg=None):
    cfg = cfg or wc.default_config()
    seed = wst.load_seed(pid)
    pos = wst.load_position(pid)
    if not seed or not pos:
        return None
    fill0 = seed["simulated_initial_fill"]
    exit_rec = _exit(pid)
    stream = wst.read_obs_stream(pid)

    # build tranche entries: initial + each ADD fill
    tranches = [{"label": "initial", "fill_ts": fill0.get("ts"),
                 "fill_price": fill0.get("price"), "quantity": fill0.get("qty", cfg.initial_contracts)}]
    for i, o in enumerate(_orders(pid), start=1):
        tranches.append({"label": f"addition_{i}", "fill_ts": o.get("ts"),
                         "fill_price": o.get("price"), "quantity": o.get("qty", 1)})

    completed = bool(exit_rec) or pos.get("state") == "CLOSED"
    exit_ts = exit_rec.get("ts") if exit_rec else None
    exit_px = exit_rec.get("price") if exit_rec else None
    exit_reason = exit_rec.get("exit_reason") if exit_rec else pos.get("exit_reason")
    exit_ep = _epoch(exit_ts)

    total_gross = 0.0
    for t in tranches:
        q, fp = t["quantity"], t["fill_price"]
        t["capital_committed_usd"] = round((fp or 0) * q * 100.0, 2)
        t["exit_timestamp"] = exit_ts
        t["exit_price"] = exit_px
        if completed and exit_px is not None and fp:
            gross = (exit_px - fp) * q * 100.0
            net = gross - FEE_PER_CONTRACT * q * 2      # placeholder round-trip fee
            t["gross_pnl_usd"] = round(gross, 2)
            t["net_pnl_usd_est"] = round(net, 2)
            total_gross += gross
        else:
            t["gross_pnl_usd"] = None
            t["net_pnl_usd_est"] = None
        # MFE / MAE over this tranche's holding window from the option-bid path
        fep = _epoch(t["fill_ts"])
        mfe = mae = None
        for o in stream:
            ots = _epoch(o.get("ts"))
            bid = o.get("option_bid")
            if ots is None or bid is None or fep is None:
                continue
            if ots < fep or (exit_ep is not None and ots > exit_ep):
                continue
            r = (bid - fp) / fp if fp else 0.0
            mfe = r if mfe is None else max(mfe, r)
            mae = r if mae is None else min(mae, r)
        t["mfe_pct"] = (round(mfe, 6) if mfe is not None else None)
        t["mae_pct"] = (round(mae, 6) if mae is not None else None)
        t["time_held_seconds"] = (round(exit_ep - fep, 1) if (exit_ep and fep) else None)
        t["seconds_from_fill_to_exit"] = t["time_held_seconds"]

    # contributions + incremental attribution
    for t in tranches:
        g = t["gross_pnl_usd"]
        t["contribution_to_total_pnl_pct"] = (round(g / total_gross, 4)
                                              if (g is not None and total_gross) else None)
    adds = [t for t in tranches if t["label"] != "initial"]
    incr_gross = sum(t["gross_pnl_usd"] for t in adds if t["gross_pnl_usd"] is not None)
    incr_net = sum(t["net_pnl_usd_est"] for t in adds if t["net_pnl_usd_est"] is not None)
    incr_cap = sum(t["capital_committed_usd"] for t in adds)
    for t in adds:
        t["addition_improved_result"] = (None if t["net_pnl_usd_est"] is None
                                         else t["net_pnl_usd_est"] > 0)
    final_add_ts = adds[-1]["fill_ts"] if adds else None

    occ = parse_occ(pos.get("contract_symbol"))
    activation = _epoch(seed.get("activation_timestamp"))
    dte = ((occ["expiry"] - datetime.fromtimestamp(activation, tz=timezone.utc).date()).days
           if (occ and activation) else None)

    return {
        "position_id": pid, "cohort_id": COHORT_ID,
        "direction": pos.get("direction"),
        "underlying_symbol": pos.get("underlying_symbol"),
        "contract_symbol": pos.get("contract_symbol"),
        "completed": completed, "exit_reason": exit_reason,
        "tranches": tranches,
        "num_additions": len(adds),
        "total_gross_pnl_usd": round(total_gross, 2) if completed else None,
        "incremental_pnl_from_additions_usd": round(incr_gross, 2) if adds else 0.0,
        "incremental_net_pnl_from_additions_usd": round(incr_net, 2) if adds else 0.0,
        "capital_added_by_additions_usd": round(incr_cap, 2),
        "incremental_pnl_per_added_dollar": (round(incr_gross / incr_cap, 4) if incr_cap else None),
        "seconds_from_each_add_to_exit": [t["seconds_from_fill_to_exit"] for t in adds],
        "seconds_from_final_add_to_exit": (round(exit_ep - _epoch(final_add_ts), 1)
                                           if (exit_ep and final_add_ts) else None),
        # descriptive dimensions (delta unavailable in observer-v0 — moneyness proxy)
        "dte_days": dte,
        "seed_spread_pct": (seed.get("option_quote", {}).get("ask", 0) and
                            round((seed["option_quote"]["ask"] - seed["option_quote"]["bid"])
                                  / (((seed["option_quote"]["ask"] + seed["option_quote"]["bid"]) / 2) or 1)
                                  * 100, 3)),
        "delta_bucket": "unavailable_in_observer_v0",
        "market_date": (datetime.fromtimestamp(activation, tz=timezone.utc).date().isoformat()
                        if activation else None),
    }


# ── lightweight progress (for the dashboard accumulation display) ───────────

def cohort_progress(pids=None, cfg=None):
    """Just the acceptance-gate counts + frozen config hash. Read-only; does not
    change the cohort schema or any behavior."""
    cfg = cfg or wc.default_config()
    pids = pids if pids is not None else _pids_from_positions()
    reports = [r for r in (position_tranches(p, cfg) for p in pids) if r]
    completed = [r for r in reports if r["completed"]]
    sessions = {r["market_date"] for r in reports if r["market_date"]}
    symbols = {r["underlying_symbol"] for r in reports if r["underlying_symbol"]}
    calls = sum(1 for r in completed if r["direction"] == "CALL")
    puts = sum(1 for r in completed if r["direction"] == "PUT")
    T = COHORT_TARGET
    prog = {"cohort_id": COHORT_ID, "config_hash": fe.canonical_hash(cfg.to_dict()),
            "completed_simulations": len(completed), "target_simulations": T["min_simulations"],
            "sessions": len(sessions), "target_sessions": T["min_sessions"],
            "calls": calls, "target_calls": T["min_calls"],
            "puts": puts, "target_puts": T["min_puts"],
            "symbols": len(symbols), "target_symbols": T["min_symbols"]}
    prog["cohort_complete"] = (len(completed) >= T["min_simulations"]
                               and len(sessions) >= T["min_sessions"]
                               and calls >= T["min_calls"] and puts >= T["min_puts"]
                               and len(symbols) >= T["min_symbols"])
    return prog


# ── cohort aggregation (8 sections) ─────────────────────────────────────────

def _pids_from_positions(base=None):
    import json
    import os
    base = base or wst.POSITION_DIR
    if not os.path.isdir(base):
        return []
    pids = []
    for fn in sorted(os.listdir(base)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(base, fn)) as handle:
                position = json.load(handle)
        except (OSError, ValueError):
            continue
        cohort_id = position.get("research_cohort_id")
        if cohort_id in (None, COHORT_ID):
            pids.append(fn[:-5])
    return pids


def _bucket_spread(sp):
    if sp is None:
        return "unknown"
    return "<=1%" if sp <= 1 else ("1-3%" if sp <= 3 else ("3-5%" if sp <= 5 else ">5%"))


def _bucket_dte(d):
    if d is None:
        return "unknown"
    return "0DTE" if d <= 0 else ("1-7" if d <= 7 else ("8-30" if d <= 30 else ">30"))


def build_cohort_report(pids=None, cfg=None):
    cfg = cfg or wc.default_config()
    pids = pids if pids is not None else _pids_from_positions()
    reports = [r for r in (position_tranches(p, cfg) for p in pids) if r]
    completed = [r for r in reports if r["completed"]]

    # 1. operational integrity
    sessions = sorted({r["market_date"] for r in reports if r["market_date"]})
    symbols = sorted({r["underlying_symbol"] for r in reports if r["underlying_symbol"]})
    calls = [r for r in completed if r["direction"] == "CALL"]
    puts = [r for r in completed if r["direction"] == "PUT"]
    accept = {
        "completed_simulations": len(completed),
        "sessions": len(sessions), "call_sims": len(calls), "put_sims": len(puts),
        "symbols": len(symbols),
        "meets_min_simulations": len(completed) >= COHORT_TARGET["min_simulations"],
        "meets_min_sessions": len(sessions) >= COHORT_TARGET["min_sessions"],
        "meets_min_calls": len(calls) >= COHORT_TARGET["min_calls"],
        "meets_min_puts": len(puts) >= COHORT_TARGET["min_puts"],
        "meets_min_symbols": len(symbols) >= COHORT_TARGET["min_symbols"],
    }
    accept["cohort_complete"] = all(accept[k] for k in
                                    ("meets_min_simulations", "meets_min_sessions",
                                     "meets_min_calls", "meets_min_puts", "meets_min_symbols"))

    # 2. synchronization & quote quality (from observation streams)
    sync_total = sync_unsync = q_ok = q_bad = 0
    for p in pids:
        for o in wst.read_obs_stream(p):
            sync_total += 1
            if o.get("unsynchronized"):
                sync_unsync += 1
            if o.get("quote_quality") in ("crossed", "missing", "unusable", "stale"):
                q_bad += 1
            else:
                q_ok += 1
    sync_section = {"observations": sync_total,
                    "unsynchronized_rate": round(sync_unsync / sync_total, 4) if sync_total else None,
                    "bad_quote_rate": round(q_bad / sync_total, 4) if sync_total else None}

    # 3. add-trigger behavior / 4. reversal behavior
    add_counts = _tally(r["num_additions"] for r in reports)
    reversal_reasons = _tally(r["exit_reason"] for r in completed)

    # 5. tranche-level performance
    tranche_stats = {}
    for r in completed:
        for t in r["tranches"]:
            s = tranche_stats.setdefault(t["label"], {"n": 0, "net_sum": 0.0, "improved": 0})
            s["n"] += 1
            if t.get("net_pnl_usd_est") is not None:
                s["net_sum"] += t["net_pnl_usd_est"]
            if t.get("addition_improved_result") is True:
                s["improved"] += 1
    incr_per_dollar = [r["incremental_pnl_per_added_dollar"] for r in completed
                       if r["incremental_pnl_per_added_dollar"] is not None]

    # 6/7. WR vs baseline A (ratchet) and B (hold) — from each position's stream
    wr_wins_a = wr_wins_b = compared = 0
    for p in pids:
        stream = wst.read_obs_stream(p)
        if len(stream) < 2:
            continue
        tr = wb.build_tracks(cfg, stream)
        if "wave_riding" not in tr or tr.get("error"):
            continue
        compared += 1
        w = tr["wave_riding"].get("net_pnl_usd") or 0
        if w > (tr["baseline_a"].get("net_pnl_usd") or 0):
            wr_wins_a += 1
        if w > (tr["baseline_b"].get("net_pnl_usd") or 0):
            wr_wins_b += 1

    # 8. outcomes by dimension
    by_dir = _group_net(completed, lambda r: r["direction"])
    by_ticker = _group_net(completed, lambda r: r["underlying_symbol"])
    by_dte = _group_net(completed, lambda r: _bucket_dte(r["dte_days"]))
    by_spread = _group_net(completed, lambda r: _bucket_spread(r["seed_spread_pct"]))
    by_adds = _group_net(completed, lambda r: r["num_additions"])

    return {
        "cohort_id": COHORT_ID, "report_only": True, "is_edge_claim": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_hash": fe.canonical_hash(cfg.to_dict()),
        "1_operational_integrity": accept,
        "2_synchronization_and_quote_quality": sync_section,
        "3_add_trigger_behavior": {"additions_per_sim_distribution": add_counts},
        "4_reversal_behavior": {"exit_reason_distribution": reversal_reasons},
        "5_tranche_level_performance": {
            "by_tranche": {k: {"n": v["n"], "mean_net_usd": (round(v["net_sum"] / v["n"], 2) if v["n"] else None),
                               "additions_improved": v["improved"]} for k, v in tranche_stats.items()},
            "median_incremental_pnl_per_added_dollar": _median(incr_per_dollar)},
        "6_wave_riding_vs_standard_ratchet": {"compared": compared, "wr_beats_ratchet": wr_wins_a,
                                              "baseline_policy_hash": wb.standard_policy_hash()},
        "7_wave_riding_vs_one_contract_hold": {"compared": compared, "wr_beats_hold": wr_wins_b},
        "8_outcomes_by_dimension": {"direction": by_dir, "ticker": by_ticker, "dte": by_dte,
                                    "spread": by_spread, "num_additions": by_adds,
                                    "delta": "unavailable_in_observer_v0"},
        "note": ("Operational validation only — identifies defects, harmful add "
                 "behavior, and whether a larger formal study is justified. NOT a "
                 "proof of trading edge. Net P&L uses a placeholder fee model and "
                 "shadow fills (IEX underlying / OPRA option)."),
    }


def _tally(it):
    from collections import Counter
    return dict(Counter(it))


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return round(xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2, 4)


def _group_net(reports, keyfn):
    groups = {}
    for r in reports:
        k = keyfn(r)
        g = groups.setdefault(str(k), {"n": 0, "net_sum": 0.0})
        g["n"] += 1
        nets = [t["net_pnl_usd_est"] for t in r["tranches"] if t.get("net_pnl_usd_est") is not None]
        g["net_sum"] += sum(nets)
    return {k: {"n": v["n"], "total_net_usd": round(v["net_sum"], 2),
                "mean_net_usd": round(v["net_sum"] / v["n"], 2) if v["n"] else None}
            for k, v in groups.items()}


def render_markdown(rep):
    a = rep["1_operational_integrity"]
    L = [f"# Wave Riding Operational Cohort — `{rep['cohort_id']}`", "",
         f"*Generated {rep['generated_at']} · REPORT-ONLY · **not a proof of trading "
         f"edge**. Config frozen (`{rep['config_hash'][:12]}`); no thresholds changed.*", "",
         "## 1. Operational integrity (cohort acceptance)", "",
         f"- Completed simulations: **{a['completed_simulations']} / {30}** "
         f"({'MET' if a['meets_min_simulations'] else 'not met'})",
         f"- Sessions: **{a['sessions']} / 10** ({'MET' if a['meets_min_sessions'] else 'not met'})",
         f"- CALL / PUT: **{a['call_sims']} / {a['put_sims']}** (need ≥10 each — "
         f"{'MET' if a['meets_min_calls'] and a['meets_min_puts'] else 'not met'})",
         f"- Symbols: **{a['symbols']} / 5** ({'MET' if a['meets_min_symbols'] else 'not met'})",
         f"- **Cohort complete: {a['cohort_complete']}**", "",
         "## 2. Synchronization & quote quality", "",
         f"- Observations: {rep['2_synchronization_and_quote_quality']['observations']} · "
         f"unsynchronized rate: {rep['2_synchronization_and_quote_quality']['unsynchronized_rate']} · "
         f"bad-quote rate: {rep['2_synchronization_and_quote_quality']['bad_quote_rate']}", "",
         "## 3. Add-trigger behavior", "",
         f"- Additions/sim: {rep['3_add_trigger_behavior']['additions_per_sim_distribution']}", "",
         "## 4. Reversal behavior", "",
         f"- Exit reasons: {rep['4_reversal_behavior']['exit_reason_distribution']}", "",
         "## 5. Tranche-level performance", "",
         f"- By tranche: {rep['5_tranche_level_performance']['by_tranche']}",
         f"- Median incremental P&L per added dollar: "
         f"{rep['5_tranche_level_performance']['median_incremental_pnl_per_added_dollar']}", "",
         "## 6. Wave Riding vs Standard-ratchet baseline", "",
         f"- {rep['6_wave_riding_vs_standard_ratchet']}", "",
         "## 7. Wave Riding vs one-contract hold", "",
         f"- {rep['7_wave_riding_vs_one_contract_hold']}", "",
         "## 8. Outcomes by dimension", "",
         f"- Direction: {rep['8_outcomes_by_dimension']['direction']}",
         f"- Ticker: {rep['8_outcomes_by_dimension']['ticker']}",
         f"- DTE: {rep['8_outcomes_by_dimension']['dte']}",
         f"- Spread: {rep['8_outcomes_by_dimension']['spread']}",
         f"- Num additions: {rep['8_outcomes_by_dimension']['num_additions']}",
         f"- Delta: {rep['8_outcomes_by_dimension']['delta']}", "",
         "---", "", rep["note"], ""]
    return "\n".join(L)


if __name__ == "__main__":
    freeze_cohort_config()
    rep = build_cohort_report()
    with open("COHORT_A_REPORT.md", "w") as f:
        f.write(render_markdown(rep))
    print(render_markdown(rep))
