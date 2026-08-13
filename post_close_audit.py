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
from datetime import date
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


def count_day(rows: list[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    return [row for row in rows if _day(row) == day]


def direction_state_summary(decision_rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = collections.Counter()
    for row in decision_rows:
        scores = row.get("scores") or {}
        direction = scores.get("direction") or {}
        status = str(direction.get("status") or "MISSING").upper()
        if status in {"FRESH", "STALE", "MISSING", "UNAVAILABLE", "UNUSABLE"}:
            counts[status] += 1
        else:
            counts["UNUSABLE"] += 1
    total = sum(counts.values())
    fresh_rate = (counts["FRESH"] / total) if total else None
    return {
        "FRESH": counts["FRESH"],
        "STALE": counts["STALE"],
        "MISSING": counts["MISSING"],
        "UNAVAILABLE": counts["UNAVAILABLE"],
        "UNUSABLE": counts["UNUSABLE"],
        "fresh_rate": fresh_rate,
        "total": total,
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
    runtime_health = _read_jsonl(ENTRY_FILES["runtime_health"])

    audit_day = args.day or latest_day(decisions) or latest_day(episodes) or latest_day(no_trade)
    if audit_day is None:
        raise SystemExit("no dated evidence available")

    d = count_day(decisions, audit_day)
    e = count_day(episodes, audit_day)
    nt = count_day(no_trade, audit_day)
    o = count_day(outcomes, audit_day)
    ho = count_day(hypo_outcomes, audit_day)
    bt = count_day(bid_ticks, audit_day)
    hbt = count_day(hypo_bid_ticks, audit_day)
    selected = [row for row in d if row.get("selected_contract")]

    print(json.dumps({
        "audit_day": audit_day,
        "decision_packets": len(d),
        "admitted_episodes": len(e),
        "trackable_plans": sum(1 for row in nt if row.get("status") == "TRACKABLE"),
        "untrackable_plans": sum(1 for row in nt if row.get("status") == "UNTRACKABLE"),
        "untrackable_reasons": dict(collections.Counter(row.get("reason_code") for row in nt)),
        "selected_eligible_contracts": len(selected),
        "executive_bid_ticks": len(bt),
        "completed_outcomes": len(o),
        "hypothetical_bid_ticks": len(hbt),
        "hypothetical_outcomes": len(ho),
        "executive_bid_coverage": bid_coverage(o),
        "direction_axis": direction_state_summary(d),
        "collector": {
            "enabled": collector.get("enabled"),
            "config_version": collector.get("config_version"),
            "collection_role": collector.get("collection_role"),
            "cohorts_locked": collector.get("cohorts_locked"),
            "execution_authority": collector.get("execution_authority"),
            "guard_access": collector.get("guard_access"),
        },
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
