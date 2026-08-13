"""
standard-entry-incubation-study-v0 — REPLAY-ONLY research over Standard-mode
trades. Changes NO live Guard behavior, thresholds, order paths, or Standard
mode. It answers: is the current ratchet (a) exiting strong trades during normal
early volatility, (b) letting weak trades fall too far before exiting, or (c)
both under different conditions?

It replays each trade's OPTION executable-price path through arming-policy
variants A–H, using the SAME frozen entry, quantity, tick path, executable-price
assumptions, and costs for every variant. The current live Guard is never
touched; this is a pure function over a recorded path.

IMPORTANT DATA NOTE: a faithful replay requires the OPTION's executable price
path from entry through a recovery window. The existing logs do NOT contain
option tick paths (the tick log is the SPY underlying only) and the journal has
no entry timestamp — so retrospectively this engine has near-zero coverage. It
is validated on synthetic paths and becomes runnable once ENTRY_INCUBATION_SHADOW
captures real option paths going forward. See data_coverage().
"""
import math

STUDY_VERSION = "standard-entry-incubation-study-v0"
HARD_STOP_VERSION = "incubation-hard-stop-v0"

# Mirrors the live Guard rungs (profit-activation + allowed-giveback). Kept in
# lockstep with scalp_server.Guard; this is a re-implementation for REPLAY only.
DEFAULT_LADDER = [{"at": 0, "tol": 15}, {"at": 15, "tol": 6}, {"at": 30, "tol": 2.5}]
CONFIRM_TICKS = 2
GRACE_SEC = 60.0
SLIPPAGE = 0.02
# Versioned hard max-loss stop used DURING incubation so no variant is ever
# left fully unprotected (per the study's safety requirement).
HARD_STOP_PCT = 25.0
# a later executable value this many percentage points above the Current exit
# counts as a "material" recovery (PREMATURE_EXIT).
PREMATURE_MARGIN_PP = 10.0

VARIANTS = ["CURRENT", "TIME_DELAY_5M", "TIME_DELAY_10M", "PROFIT_BUFFER_5",
            "PROFIT_BUFFER_10", "TIME_OR_PROFIT", "MOMENTUM_CONFIRMED",
            "INCUBATION_WITH_HARD_STOP"]


def _ret(bid, entry):
    return (bid - entry) / entry * 100.0 if entry else 0.0


def _tol(peak, ladder):
    tol = ladder[0]["tol"]
    for r in ladder:
        if peak >= r["at"]:
            tol = r["tol"]
    return tol


def _activation_index(variant, entry, path):
    """Index at which the DYNAMIC ratchet turns on for a variant, or None if its
    trigger is never met on this path (ratchet never arms → only the hard stop
    protects, exposing the harm of that policy on losers)."""
    if variant == "CURRENT":
        return 0
    for i, p in enumerate(path):
        t, r = p["t"], _ret(p["bid"], entry)
        if variant in ("TIME_DELAY_5M", "INCUBATION_WITH_HARD_STOP") and t >= 300:
            return i
        if variant == "TIME_DELAY_10M" and t >= 600:
            return i
        if variant == "PROFIT_BUFFER_5" and r >= 5:
            return i
        if variant == "PROFIT_BUFFER_10" and r >= 10:
            return i
        if variant == "TIME_OR_PROFIT" and (t >= 300 or r >= 10):
            return i
        if variant == "MOMENTUM_CONFIRMED" and r > 0 and i > 0 and \
                _ret(path[i - 1]["bid"], entry) < r:
            return i
    return None


def _run_ratchet(path, entry, start_i, ladder, slippage):
    """Run the ratchet from `start_i` with the peak re-armed from the return at
    activation and a fresh grace window (matches the live re-engage behavior)."""
    peak = _ret(path[start_i]["bid"], entry)
    opened_t = path[start_i]["t"]
    breach = 0
    for j in range(start_i, len(path)):
        r = _ret(path[j]["bid"], entry)
        if r > peak:
            peak, breach = r, 0
            continue
        if path[j]["t"] - opened_t < GRACE_SEC:
            continue
        tol = _tol(peak, ladder)
        if r <= peak - tol:
            breach += 1
            if breach >= CONFIRM_TICKS:
                return j, f"ratchet_dip_tol{tol}", max(0.0, path[j]["bid"] - slippage)
            continue
        breach = 0
    last = path[-1]
    return len(path) - 1, "terminal", max(0.0, last["bid"] - slippage)


def replay_variant(variant, entry, path, qty=1, ladder=None, hard_stop_pct=HARD_STOP_PCT,
                   slippage=SLIPPAGE):
    """Replay one arming policy over one option executable-price path.
    `path`: [{"t": seconds_from_entry, "bid": executable_option_bid}, ...] sorted.
    Returns the full per-variant record."""
    ladder = ladder or DEFAULT_LADDER
    if not path:
        return {"variant": variant, "available": False, "reason": "no_path"}
    act_i = _activation_index(variant, entry, path)

    # policy exit (ratchet from activation, or terminal if the trigger never fires)
    if act_i is None:
        policy_i, policy_reason = len(path) - 1, "terminal_ratchet_never_armed"
    else:
        policy_i, policy_reason, _ = _run_ratchet(path, entry, act_i, ladder, slippage)

    # GLOBAL hard-stop floor, retained THROUGHOUT, applied UNIFORMLY to every
    # activation policy as a study dimension. hard_stop_pct=None means NO floor —
    # that is the CURRENT_CONFIGURED scenario (the live Guard has no separate hard
    # stop; verified). Subordinate ONLY to emergency / data-safety (checked last).
    hs_i = None
    if hard_stop_pct is not None:
        for k, p in enumerate(path):
            if _ret(p["bid"], entry) <= -hard_stop_pct:
                hs_i = k
                break
    emerg_i = None
    for k, p in enumerate(path):
        if p.get("emergency") or p.get("data_safety_exit"):
            emerg_i = k
            break

    exit_i, exit_reason = policy_i, policy_reason
    if hs_i is not None and hs_i <= exit_i:
        exit_i, exit_reason = hs_i, f"hard_stop_{hard_stop_pct}pct"
    if emerg_i is not None and emerg_i <= exit_i:      # highest priority
        exit_i, exit_reason = emerg_i, "emergency_exit"
    exit_px = max(0.0, path[exit_i]["bid"] - slippage)
    act_reason = ("immediate" if variant == "CURRENT"
                  else ("trigger_never_met" if act_i is None
                        else f"activated@t={path[act_i]['t']}s,ret={_ret(path[act_i]['bid'],entry):.1f}%"))
    return _finalize(variant, entry, path, qty, exit_i, exit_reason, exit_px,
                     activation_i=act_i, activation_reason=act_reason)


def _finalize(variant, entry, path, qty, exit_i, exit_reason, exit_px, activation_i,
              activation_reason):
    rets = [_ret(p["bid"], entry) for p in path]
    peak = max(rets)
    realized = (exit_px - entry) / entry * 100.0
    realized_pnl = (exit_px - entry) * qty * 100.0
    retained = (realized / peak) if peak > 0 else None
    upto = rets[: exit_i + 1] or [0.0]
    return {
        "variant": variant, "available": True,
        "study_version": STUDY_VERSION,
        "activation_timestamp_s": (path[activation_i]["t"] if activation_i is not None else None),
        "activation_reason": activation_reason,
        "exit_timestamp_s": path[exit_i]["t"],
        "exit_reason": exit_reason,
        "exit_price": round(exit_px, 4),
        "realized_return_pct": round(realized, 4),
        "realized_pnl_usd": round(realized_pnl, 2),
        "peak_return_pct": round(peak, 4),
        "profit_retained_from_peak": (round(retained, 4) if retained is not None else None),
        "mfe_pct": round(max(rets), 4), "mae_pct": round(min(rets), 4),
        "time_in_trade_s": path[exit_i]["t"],
    }


def replay_trade(trade, path, ladder=None, hard_stop_pct=HARD_STOP_PCT, slippage=SLIPPAGE):
    """Replay all variants for one trade over its option path (which should extend
    PAST the Current exit so recoveries are visible). Adds the two diagnostics."""
    entry = trade["entry"]
    qty = trade.get("qty", 1)
    out = {v: replay_variant(v, entry, path, qty, ladder, hard_stop_pct, slippage)
           for v in VARIANTS}
    cur = out["CURRENT"]

    # PREMATURE_EXIT: Current exited, but the option later reached a materially
    # higher executable value than the Current exit return.
    premature = None
    if cur.get("available"):
        cexit_t = cur["exit_timestamp_s"]
        cexit_ret = cur["realized_return_pct"]
        after = [_ret(p["bid"], entry) for p in path if p["t"] > cexit_t]
        if after:
            later_max = max(after)
            if later_max - cexit_ret >= PREMATURE_MARGIN_PP:
                premature = {"flag": "PREMATURE_EXIT",
                             "current_exit_return_pct": cexit_ret,
                             "later_max_return_pct": round(later_max, 4),
                             "opportunity_missed_pp": round(later_max - cexit_ret, 4)}

    # LOOSE_INITIAL_PROTECTION: trade never developed momentum (peak < 15%, the
    # initial-rung boundary) and Current took a materially larger loss than the
    # best alternative variant.
    loose = None
    peak = cur.get("peak_return_pct", 0) if cur.get("available") else 0
    if cur.get("available") and peak < 15 and cur["realized_return_pct"] < 0:
        alt_best = max((out[v]["realized_return_pct"] for v in VARIANTS
                        if v != "CURRENT" and out[v].get("available")), default=None)
        if alt_best is not None and alt_best - cur["realized_return_pct"] >= PREMATURE_MARGIN_PP:
            loose = {"flag": "LOOSE_INITIAL_PROTECTION",
                     "current_realized_pct": cur["realized_return_pct"],
                     "best_alternative_pct": round(alt_best, 4),
                     "extra_loss_vs_alternative_pp": round(alt_best - cur["realized_return_pct"], 4)}

    return {"symbol": trade.get("symbol"), "entry": entry, "qty": qty,
            "variants": out, "PREMATURE_EXIT": premature,
            "LOOSE_INITIAL_PROTECTION": loose}


# ── factorial: activation policy × hard-stop scenario (same canonical path) ─

def replay_factorial(trade, path, activations, hard_stop_scenarios, ladder=None,
                     slippage=SLIPPAGE, baseline_activation="CURRENT",
                     baseline_hard_stop="CURRENT_CONFIGURED_HARD_STOP",
                     recovery_materiality_pp=10.0, loose_materiality_pp=10.0):
    """Replay the full activation × hard-stop grid on ONE canonical path. Every
    cell reports BOTH benefit and harm vs the primary baseline (CURRENT + actual
    configured hard stop), so the larger grid can't manufacture a misleading
    'best result'. `hard_stop_scenarios` maps name → pct (None = no floor)."""
    entry, qty = trade["entry"], trade.get("qty", 1)
    grid = {}
    for a in activations:
        for hs_name, hs_pct in hard_stop_scenarios.items():
            grid[(a, hs_name)] = replay_variant(a, entry, path, qty, ladder, hs_pct, slippage)
    base = grid[(baseline_activation, baseline_hard_stop)]
    b_ret, b_pnl, b_mae = (base["realized_return_pct"], base["realized_pnl_usd"], base["mae_pct"])

    def _recovers_after(exit_t, exit_ret):
        after = [_ret(p["bid"], entry) for p in path if p["t"] > exit_t]
        return (max(after) - exit_ret) if after else None

    cells = []
    for (a, hs), r in grid.items():
        d_ret = r["realized_return_pct"] - b_ret
        cut = False
        if str(r["exit_reason"]).startswith("hard_stop"):
            rec = _recovers_after(r["exit_timestamp_s"], r["realized_return_pct"])
            cut = rec is not None and rec >= recovery_materiality_pp
        cells.append({
            "activation": a, "hard_stop": hs,
            "realized_return_pct": r["realized_return_pct"],
            "realized_pnl_usd": r["realized_pnl_usd"],
            "exit_reason": r["exit_reason"], "peak_return_pct": r["peak_return_pct"],
            "mae_pct": r["mae_pct"], "profit_retained_from_peak": r["profit_retained_from_peak"],
            # BENEFIT
            "net_pnl_improvement_usd": round(r["realized_pnl_usd"] - b_pnl, 2),
            "recovery_value_captured_pp": round(max(0.0, d_ret), 4),
            # HARM
            "additional_loss_pp": round(min(0.0, d_ret), 4),
            "additional_drawdown_pp": round(r["mae_pct"] - b_mae, 4),
            "worsened_vs_baseline": d_ret < -1e-9,
            "strong_trade_cut_by_tighter_stop": cut,
            "is_baseline": (a == baseline_activation and hs == baseline_hard_stop),
        })

    # per-trade diagnostics relative to the baseline
    base_rec = _recovers_after(base["exit_timestamp_s"], b_ret)
    premature = None
    if base_rec is not None and base_rec >= recovery_materiality_pp:
        premature = {"flag": "PREMATURE_EXIT", "baseline_exit_return_pct": b_ret,
                     "later_recovery_pp": round(base_rec, 4)}
    loose = None
    if b_ret < 0:
        better = [c for c in cells if c["realized_return_pct"] - b_ret >= loose_materiality_pp]
        recovered = base_rec is not None and base_rec >= recovery_materiality_pp
        if better and not recovered:
            loose = {"flag": "LOOSE_INITIAL_PROTECTION", "baseline_loss_pct": b_ret,
                     "best_safer_pct": round(max(c["realized_return_pct"] for c in better), 4)}

    return {"symbol": trade.get("symbol"), "entry": entry, "qty": qty,
            "baseline_key": [baseline_activation, baseline_hard_stop],
            "cells": cells, "PREMATURE_EXIT": premature,
            "LOOSE_INITIAL_PROTECTION": loose}


# ── data coverage over the EXISTING logs (the honest gate) ──────────────────

def data_coverage(journal_path="scalp_journal.csv", tick_log_path="tick_log.csv"):
    """How many journal trades can actually be replayed? Requires an OPTION tick
    path per trade. Reports the gap explicitly."""
    import csv
    import collections
    trades = list(csv.DictReader(open(journal_path))) if _exists(journal_path) else []
    trades = [t for t in trades if t.get("scope_class") != "manual_out_of_envelope"]
    tick_syms = set()
    n_ticks = 0
    if _exists(tick_log_path):
        with open(tick_log_path) as f:
            rd = csv.DictReader(f)
            for r in rd:
                n_ticks += 1
                tick_syms.add(r.get("symbol"))
    traded_syms = collections.Counter(t.get("symbol") for t in trades)
    option_syms = {s for s in traded_syms if s and len(s) > 8}
    have_option_paths = option_syms & tick_syms
    return {
        "study_version": STUDY_VERSION,
        "journal_trades": len(trades),
        "distinct_option_contracts_traded": len(option_syms),
        "tick_log_rows": n_ticks,
        "tick_log_symbols": sorted(tick_syms),
        "option_contracts_with_tick_history": sorted(have_option_paths),
        "trades_replayable_now": 0 if not have_option_paths else None,
        "journal_has_entry_timestamp": _journal_has_entry_ts(trades),
        "verdict": ("NOT REPLAYABLE retrospectively: the tick log is underlying-only "
                    "(no option contract quote history) and the journal stores only "
                    "trade endpoints (no entry timestamp, no intra-trade path). A "
                    "faithful replay needs the option executable-price path from entry "
                    "through a recovery window. Capture it going forward via "
                    "ENTRY_INCUBATION_SHADOW."),
    }


def _journal_has_entry_ts(trades):
    if not trades:
        return False
    cols = set(trades[0].keys())
    return bool({"entry_time", "opened", "open_time"} & cols)


def _exists(p):
    import os
    return os.path.exists(p)


# ── endpoint-only coarse diagnostic (what the journal CAN tell us) ──────────

def endpoint_only_diagnostics(journal_path="scalp_journal.csv"):
    """Without a path we cannot assess PREMATURE_EXIT (needs post-exit data). But
    LOOSE_INITIAL_PROTECTION is partly visible from endpoints: a trade whose peak
    never reached the 15% initial-rung boundary and which still exited at the
    initial rung *must* have exited at a loss (sell level = peak − 15% < 0). We
    can count those; we cannot say whether they would have recovered."""
    import csv
    import re
    trades = [t for t in csv.DictReader(open(journal_path))
              if t.get("scope_class") != "manual_out_of_envelope"]
    loose = 0
    loose_loss_sum = 0.0
    total = 0
    for t in trades:
        try:
            peak = float(t["peak_pct"])
            real = float(t["realized_pct"])
        except Exception:
            continue
        total += 1
        m = re.search(r"tol\s*([\d.]+)%", t.get("reason", ""))
        tol = m.group(1) if m else None
        if tol == "15" and peak < 15 and real < 0:
            loose += 1
            loose_loss_sum += real
    return {
        "trades_scored": total,
        "LOOSE_INITIAL_PROTECTION_candidates": loose,
        "mean_loss_of_those_pct": round(loose_loss_sum / loose, 3) if loose else None,
        "note": ("Endpoint-only. Counts trades that exited at the initial 15% rung "
                 "with peak < 15% and a loss — structurally guaranteed losses. Cannot "
                 "assess PREMATURE_EXIT or recoveries without an option price path."),
    }
