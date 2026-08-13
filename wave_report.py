"""
Wave Riding shadow outcome + comparison report. Operational + experimental.

Aggregates the shadow audit logs (observations / orders / exits) and, when given
a list of three-track comparison results from `wave_baselines.build_tracks`,
summarizes the §22 comparison and §28 promotion metrics. Non-qualifying: no edge
claim, no probability, no ML. Starting thresholds are experimental research
parameters, not validated trading rules.
"""
from collections import Counter

import feature_engine as fe
import wave_store as ws


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return round(xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2, 4)


def build_report(tracks=None, observations_path=ws.OBSERVATIONS_LOG,
                 orders_path=ws.ORDERS_LOG, exits_path=ws.EXITS_LOG):
    obs = list(fe._iter_jsonl(observations_path))
    orders = list(fe._iter_jsonl(orders_path))
    exits = list(fe._iter_jsonl(exits_path))

    # blocked-add reason distribution across observations
    reason_counts = Counter()
    states = Counter()
    for o in obs:
        states[o.get("state")] += 1
        for rc in (o.get("reason_codes") or []):
            reason_counts[rc] += 1

    adds_filled = sum(1 for r in orders if r.get("action") == "ADD" and r.get("filled"))
    exits_filled = sum(1 for r in exits if r.get("filled"))

    track_summary = None
    if tracks:
        wr_pnls = [t["wave_riding"]["net_pnl_usd"] for t in tracks
                   if t.get("wave_riding", {}).get("net_pnl_usd") is not None]
        a_pnls = [t["baseline_a"]["net_pnl_usd"] for t in tracks if t.get("baseline_a")]
        b_pnls = [t["baseline_b"]["net_pnl_usd"] for t in tracks if t.get("baseline_b")]
        adds_per = [t["wave_riding"].get("num_adds", 0) for t in tracks if t.get("wave_riding")]
        track_summary = {
            "tracks": len(tracks),
            "median_net_pnl_usd": {"wave_riding": _median(wr_pnls),
                                   "baseline_a_ratchet": _median(a_pnls),
                                   "baseline_b_hold": _median(b_pnls)},
            "median_adds_per_wave_riding_trade": _median(adds_per),
            "wave_riding_beats_ratchet_count": sum(
                1 for t in tracks
                if (t.get("wave_riding", {}).get("net_pnl_usd") or 0)
                > (t.get("baseline_a", {}).get("net_pnl_usd") or 0)),
        }

    return {
        "report_version": "wave-riding-v0",
        "evaluation_status": "operational_experimental",
        "formal_cohort_eligible": False,
        "shadow_only": True,
        "observations_logged": len(obs),
        "orders_logged": len(orders),
        "adds_filled": adds_filled,
        "exits_filled": exits_filled,
        "state_distribution": dict(states),
        "add_blocker_reason_distribution": dict(reason_counts),
        "track_comparison": track_summary,
        "note": ("Shadow-only, experimental. Judge Wave Riding against incremental "
                 "capital, drawdown, slippage, and tail loss — not gross profit "
                 "alone. Thresholds are research starting points, not validated."),
    }
