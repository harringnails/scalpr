"""Read-only post-close audit helper for Scalpr evidence logs.

This script computes the same maturity metrics used in the manual audit:
- Entry Intelligence daily counts
- Trackable vs untrackable plans
- bid/outcome coverage
- post-warmup direction-state distribution
- simple feed sanity markers

It never writes to the trading system.
"""

from __future__ import annotations

import argparse
import collections
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
ENTRY_FILES = {
    "decision_packets": ROOT / "entry_intelligence_decisions_v1.jsonl",
    "episodes": ROOT / "entry_intelligence_episodes_v1.jsonl",
    "no_trade_plans": ROOT / "entry_intelligence_no_trade_plans_v1.jsonl",
    "outcomes": ROOT / "entry_intelligence_outcomes_v1.jsonl",
    "hypothetical_outcomes": ROOT / "entry_intelligence_hypothetical_outcomes_v1.jsonl",
    "bid_ticks": ROOT / "entry_intelligence_bid_ticks_v1.jsonl",
    "hypothetical_bid_ticks": ROOT / "entry_intelligence_hypothetical_bid_ticks_v1.jsonl",
    "collector_status": ROOT / "entry_intelligence_collector_status_v1.json",
    "episode_quarantine": ROOT / "entry_intelligence_episode_quarantine_v1.jsonl",
    "tick_log": ROOT / "tick_log.csv",
    "runtime_health": ROOT / "runtime_health_v1.jsonl",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _day(row: dict[str, Any]) -> str | None:
    for key in ("observed_at", "decided_at", "created_at", "emitted_at", "received_at"):
        value = row.get(key)
        if value:
            return str(value)[:10]
    return None


def latest_day(rows: list[dict[str, Any]]) -> str | None:
    days = [day for day in (_day(row) for row in rows) if day]
    return max(days) if days else None


def count_day(
    rows: list[dict[str, Any]],
    day: str,
    *,
    decision_days: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Select rows by their parent decision day when linkage is available."""
    if decision_days is None:
        return [row for row in rows if _day(row) == day]
    return [
        row for row in rows
        if decision_days.get(str(row.get("decision_id") or "")) == day
    ]


def decision_day_index(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(row["decision_id"]): row_day
        for row in rows
        if row.get("decision_id") and (row_day := _day(row))
    }


def latest_per_decision(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the last append-only lifecycle revision for each decision."""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        decision_id = str(row.get("decision_id") or "")
        if decision_id:
            latest[decision_id] = row
    return list(latest.values())


TERMINAL_OUTCOME_STATUSES = {
    "FINAL",
    "UNLABELABLE_NO_EXECUTABLE_BIDS",
    "UNLABELABLE_INSUFFICIENT_COVERAGE",
}


def net_return_sensitivity_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"0_ticks": 0, "1_tick": 0, "2_ticks": 0}
    for row in rows:
        values = row.get("net_return_fraction_by_slippage_ticks_per_side") or {}
        for raw_key, output_key in (("0", "0_ticks"), ("1", "1_tick"), ("2", "2_ticks")):
            if values.get(raw_key) is not None:
                counts[output_key] += 1
    return counts


def direction_state_summary(decision_rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = collections.Counter()
    warmup_count = 0
    evaluated_rows = 0
    for row in decision_rows:
        scores = row.get("scores") or {}
        direction = scores.get("direction") or {}
        unavailable = {str(item) for item in (direction.get("unavailable") or [])}
        missing_or_stale = {str(item) for item in (row.get("missing_or_stale") or [])}
        is_warmup = "completed_5m_bars<15" in unavailable or "completed_5m_bars<15" in missing_or_stale
        if is_warmup:
            warmup_count += 1
            continue
        status = str(direction.get("status") or "MISSING").upper()
        if status in {"FRESH", "STALE", "MISSING", "UNAVAILABLE", "UNUSABLE"}:
            counts[status] += 1
        else:
            counts["UNUSABLE"] += 1
        evaluated_rows += 1
    fresh_rate = (counts["FRESH"] / evaluated_rows) if evaluated_rows else None
    return {
        "FRESH": counts["FRESH"],
        "STALE": counts["STALE"],
        "MISSING": counts["MISSING"],
        "UNAVAILABLE": counts["UNAVAILABLE"],
        "UNUSABLE": counts["UNUSABLE"],
        "warmup_missing": warmup_count,
        "fresh_rate": fresh_rate,
        "total": evaluated_rows,
        "post_warmup_total": evaluated_rows,
    }


def bid_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"coverage": None, "max_gap_seconds": None}
    fractions = []
    gaps = []
    for row in rows:
        coverage = row.get("coverage") or {}
        if coverage.get("fraction") is not None:
            fractions.append(float(coverage["fraction"]))
        if coverage.get("max_gap_seconds") is not None:
            gaps.append(float(coverage["max_gap_seconds"]))
    return {
        "coverage": min(fractions) if fractions else None,
        "max_gap_seconds": max(gaps) if gaps else None,
    }


def a2_accrual_scoreboard(summary: dict[str, Any] | None) -> dict[str, Any]:
    """Expose the MVP basis without mixing it with option-bid trackability."""
    if not summary:
        return {
            "basis": "underlying_forward_return_a2_mvp_edge",
            "status": "A2_MEASUREMENT_UNAVAILABLE",
            "clean_a2_labelable_episode_count": None,
        }
    if summary.get("measurement_error"):
        return {
            "basis": "underlying_forward_return_a2_mvp_edge",
            "status": "A2_MEASUREMENT_ERROR",
            "error": summary["measurement_error"],
            "clean_a2_labelable_episode_count": None,
        }
    count = summary.get("clean_a2_labelable_episode_count")
    return {
        "basis": "underlying_forward_return_a2_mvp_edge",
        "status": "MEASURED",
        "clean_a2_eligible_episode_count": summary.get("clean_a2_eligible_episode_count"),
        "clean_a2_labelable_episode_count": count,
        "clean_a2_unavailable_episode_count": summary.get("clean_a2_unavailable_episode_count"),
        "a2_unavailable_reason_counts": summary.get("missing_reason_counts", {}),
        "collision_episode_rows_excluded": summary.get("n_collision_episode_rows_excluded"),
        "quarantined_episode_rows_excluded": summary.get("n_quarantined_episode_rows_excluded"),
        "accrual_target_clean_a2_episodes": 200,
        "remaining_to_accrual_target": max(0, 200 - int(count or 0)),
        "data_integrity_status": summary.get("data_integrity_status"),
        "phase4_preflight": summary.get("phase4_preflight"),
        "power_gate_reached": summary.get("power_gate_reached"),
    }


def dense_source_gap_note(summary: dict[str, Any] | None) -> str | None:
    if not summary:
        return None
    reasons = summary.get("missing_reason_counts") or {}
    if not reasons:
        return None
    top_reason = max(reasons.items(), key=lambda item: item[1])[0]
    if "missing_endpoint_15m_within_5s" in top_reason or "missing_endpoint_30m_within_5s" in top_reason:
        return (
            "dense-source gap: SPY tick_log.csv lacks at least one of the 15m/30m "
            "point-in-time endpoints within the 5s window; this is the A2 blocker."
        )
    return None


def build_report(
    *,
    audit_day: str,
    decisions: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    no_trade: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    hypo_outcomes: list[dict[str, Any]],
    bid_ticks: list[dict[str, Any]],
    hypo_bid_ticks: list[dict[str, Any]],
    collector: dict[str, Any],
    quarantine: list[dict[str, Any]] | None = None,
    a2_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decision_days = decision_day_index(decisions)
    d = count_day(decisions, audit_day)
    e = count_day(episodes, audit_day, decision_days=decision_days)
    nt = count_day(no_trade, audit_day, decision_days=decision_days)
    bt = count_day(bid_ticks, audit_day, decision_days=decision_days)
    hbt = count_day(hypo_bid_ticks, audit_day, decision_days=decision_days)
    latest_outcomes = count_day(
        latest_per_decision(outcomes), audit_day, decision_days=decision_days)
    latest_hypo_outcomes = count_day(
        latest_per_decision(hypo_outcomes), audit_day, decision_days=decision_days)
    completed = [
        row for row in latest_outcomes
        if row.get("status") in TERMINAL_OUTCOME_STATUSES
    ]
    completed_hypothetical = [
        row for row in latest_hypo_outcomes
        if row.get("status") in TERMINAL_OUTCOME_STATUSES
    ]
    selected = [row for row in d if row.get("selected_contract")]
    admitted = [row for row in e if row.get("admitted") is True]
    rejected = [row for row in e if row.get("admitted") is not True]
    quarantined_ids = {
        str(row.get("episode_record_id")) for row in (quarantine or [])
        if row.get("record_type") == "QUARANTINE_ENTRY" and row.get("episode_record_id")
    }
    unquarantined_admitted = [
        row for row in admitted
        if str(row.get("episode_record_id") or "") not in quarantined_ids
    ]
    admitted_sides_by_time: dict[str, set[str]] = collections.defaultdict(set)
    for row in unquarantined_admitted:
        admitted_sides_by_time[str(row.get("decided_at") or "")].add(
            str(row.get("side") or "").upper())
    unmatched_outcomes = [
        row for row in outcomes
        if str(row.get("decision_id") or "") not in decision_days
    ]
    unmatched_hypothetical = [
        row for row in hypo_outcomes
        if str(row.get("decision_id") or "") not in decision_days
    ]

    return {
        "audit_day": audit_day,
        "entry_intelligence_ledger": {
            "decision_packets": len(d),
            "episode_evaluations": len(e),
            "admitted_episodes": len(admitted),
            "rejected_episode_evaluations": len(rejected),
            "episode_session_dates": dict(collections.Counter(
                str(row.get("session_date") or "MISSING") for row in e)),
            "session_date_mismatches": sum(
                row.get("session_date") != audit_day for row in e),
            "quarantine_sweep": {
                "admitted_quarantined": len(admitted) - len(unquarantined_admitted),
                "admitted_unquarantined": len(unquarantined_admitted),
                "unquarantined_collision_groups": sum(
                    sides == {"CALL", "PUT"}
                    for sides in admitted_sides_by_time.values()),
            },
            "episode_rejection_reasons": dict(collections.Counter(
                str(row.get("rejection_reason") or "UNSPECIFIED") for row in rejected)),
        },
        "contract_data_v2_scoreboard": {
            "basis": "single_option_executable_bid_contract_data_v2",
            "trackable_plans": sum(1 for row in nt if row.get("status") == "TRACKABLE"),
            "untrackable_plans": sum(1 for row in nt if row.get("status") == "UNTRACKABLE"),
            "untrackable_reasons": dict(collections.Counter(
                str(row.get("reason_code") or "UNSPECIFIED") for row in nt
                if row.get("status") == "UNTRACKABLE")),
            "selected_eligible_contracts": len(selected),
            "executable_bid_ticks": len(bt),
            "completed_outcomes": len(completed),
            "latest_outcome_statuses": dict(collections.Counter(
                str(row.get("status") or "MISSING") for row in latest_outcomes)),
            "hypothetical_bid_ticks": len(hbt),
            "completed_hypothetical_outcomes": len(completed_hypothetical),
            "latest_hypothetical_outcome_statuses": dict(collections.Counter(
                str(row.get("status") or "MISSING") for row in latest_hypo_outcomes)),
            "net_return_outcomes": net_return_sensitivity_counts(completed),
            "hypothetical_net_return_outcomes": net_return_sensitivity_counts(
                completed_hypothetical),
            "executable_bid_coverage": bid_coverage(completed),
            "outcome_linkage": {
                "unmatched_outcome_rows": len(unmatched_outcomes),
                "unmatched_hypothetical_outcome_rows": len(unmatched_hypothetical),
                "date_basis": "parent_decision_decided_at",
            },
        },
        "a2_accrual_scoreboard": a2_accrual_scoreboard(a2_summary),
        "dense_source_gap_note": dense_source_gap_note(a2_summary),
        "direction_axis": direction_state_summary(d),
        "collector": {
            "enabled": collector.get("enabled"),
            "config_version": collector.get("config_version"),
            "collector_version": collector.get("collector_version"),
            "collection_role": collector.get("collection_role"),
            "cohorts_locked": collector.get("cohorts_locked"),
            "execution_authority": collector.get("execution_authority"),
            "guard_access": collector.get("guard_access"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", help="Override the audit day (YYYY-MM-DD)")
    args = parser.parse_args()

    decisions = _read_jsonl(ENTRY_FILES["decision_packets"])
    episodes = _read_jsonl(ENTRY_FILES["episodes"])
    no_trade = _read_jsonl(ENTRY_FILES["no_trade_plans"])
    outcomes = _read_jsonl(ENTRY_FILES["outcomes"])
    hypo_outcomes = _read_jsonl(ENTRY_FILES["hypothetical_outcomes"])
    bid_ticks = _read_jsonl(ENTRY_FILES["bid_ticks"])
    hypo_bid_ticks = _read_jsonl(ENTRY_FILES["hypothetical_bid_ticks"])
    collector = _read_json(ENTRY_FILES["collector_status"])
    quarantine = _read_jsonl(ENTRY_FILES["episode_quarantine"])
    audit_day = args.day or latest_day(decisions) or latest_day(episodes) or latest_day(no_trade)
    if audit_day is None:
        raise SystemExit("no dated evidence available")

    try:
        import a2_measurement
        _labels, a2_summary = a2_measurement.measure_a2(
            episodes_path=ENTRY_FILES["episodes"],
            tick_log_path=ENTRY_FILES["tick_log"],
            quarantine_path=ENTRY_FILES["episode_quarantine"],
            session_date=audit_day,
        )
    except Exception as exc:
        a2_summary = {
            "measurement_error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }

    report = build_report(
        audit_day=audit_day,
        decisions=decisions,
        episodes=episodes,
        no_trade=no_trade,
        outcomes=outcomes,
        hypo_outcomes=hypo_outcomes,
        bid_ticks=bid_ticks,
        hypo_bid_ticks=hypo_bid_ticks,
        collector=collector,
        quarantine=quarantine,
        a2_summary=a2_summary,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
