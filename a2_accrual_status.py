"""Read-only A2 accrual progress and planning projection.

The 200-episode verdict gate is unchanged. Projection fields are planning-only
and assume the observed non-overlapping episode rate remains constant.
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any
from zoneinfo import ZoneInfo

import a2_accrual_store as store


SCHEMA_VERSION = "a2-accrual-status-v0"
TARGET_EPISODES = 200
NON_OVERLAP_MINUTES = 60
MIN_STABLE_EPISODES = 30
MIN_STABLE_SESSIONS = 15
V1_2_ACCRUAL_START = date(2026, 8, 14)
ET = ZoneInfo("America/New_York")
SPECIAL_XNYS_CLOSURES = {
    date(2012, 10, 29), date(2012, 10, 30), date(2018, 12, 5),
}


class AccrualStatusError(RuntimeError):
    """The dense store cannot support an honest accrual report."""


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    value = date(year, month, day)
    if value.weekday() == calendar.SATURDAY:
        return value - timedelta(days=1)
    if value.weekday() == calendar.SUNDAY:
        return value + timedelta(days=1)
    return value


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    value = date(year, month, 1)
    offset = (weekday - value.weekday()) % 7
    return value + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    value = date(year, month, calendar.monthrange(year, month)[1])
    return value - timedelta(days=(value.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter using the Meeus/Jones/Butcher algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def xnys_holidays(year: int) -> set[date]:
    holidays = {
        _nth_weekday(year, 1, calendar.MONDAY, 3),
        _nth_weekday(year, 2, calendar.MONDAY, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, calendar.MONDAY),
        _observed_fixed_holiday(year, 7, 4),
        _nth_weekday(year, 9, calendar.MONDAY, 1),
        _nth_weekday(year, 11, calendar.THURSDAY, 4),
        _observed_fixed_holiday(year, 12, 25),
    }
    if year >= 2022:
        holidays.add(_observed_fixed_holiday(year, 6, 19))
    for nominal_year in (year, year + 1):
        observed_new_year = _observed_fixed_holiday(nominal_year, 1, 1)
        if observed_new_year.year == year:
            holidays.add(observed_new_year)
    return holidays


def is_xnys_session(value: date) -> bool:
    return (
        value.weekday() < 5
        and value not in xnys_holidays(value.year)
        and value not in SPECIAL_XNYS_CLOSURES
    )


def trading_sessions(start: date, end: date) -> list[date]:
    if end < start:
        return []
    return [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
        if is_xnys_session(start + timedelta(days=offset))
    ]


def advance_trading_sessions(start: date, count: int) -> date:
    if count < 0:
        raise ValueError("count must be non-negative")
    value = start
    advanced = 0
    while advanced < count:
        value += timedelta(days=1)
        if is_xnys_session(value):
            advanced += 1
    return value


def last_completed_session(now: datetime | None = None) -> date:
    now_et = (now or datetime.now(timezone.utc)).astimezone(ET)
    candidate = now_et.date()
    if not is_xnys_session(candidate) or now_et.time() < time(16, 0):
        candidate -= timedelta(days=1)
    while not is_xnys_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _parse_timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AccrualStatusError(f"invalid_label_decided_at={value!r}") from exc
    if parsed.tzinfo is None:
        raise AccrualStatusError(f"naive_label_decided_at={value!r}")
    return parsed.astimezone(timezone.utc)


def count_non_overlapping(labels: list[dict[str, Any]]) -> dict[str, Any]:
    labelable = [row for row in labels if row.get("label_status") == "AVAILABLE"]
    ordered = sorted(labelable, key=lambda row: _parse_timestamp(row.get("decided_at")))
    seen_episode_keys: set[str] = set()
    last_kept_by_scope: dict[tuple[str, str], datetime] = {}
    kept: list[dict[str, Any]] = []
    exclusions: Counter[str] = Counter()
    for row in ordered:
        episode_key = str(row.get("episode_key") or "")
        symbol = str(row.get("symbol") or "").upper()
        side = str(row.get("side") or "").upper()
        if not episode_key or not symbol or not side:
            raise AccrualStatusError("label_missing_episode_key_symbol_or_side")
        decided_at = _parse_timestamp(row.get("decided_at"))
        if episode_key in seen_episode_keys:
            exclusions["duplicate_episode_key"] += 1
            continue
        seen_episode_keys.add(episode_key)
        scope = (symbol, side)
        last_kept = last_kept_by_scope.get(scope)
        if last_kept and decided_at < last_kept + timedelta(minutes=NON_OVERLAP_MINUTES):
            exclusions["overlapping_60m_horizon_same_symbol_side"] += 1
            continue
        kept.append(row)
        last_kept_by_scope[scope] = decided_at
    return {
        "raw_labelable_count": len(labelable),
        "non_overlapping_count": len(kept),
        "gap_count": len(labelable) - len(kept),
        "exclusion_reason_counts": dict(exclusions),
        "labels": kept,
    }


def poisson_rate_interval(
    count: int, exposure_sessions: int, confidence: float = 0.95,
) -> tuple[float, float]:
    """Approximate Garwood interval using the Byar cube-root approximation."""
    if count < 0 or exposure_sessions <= 0:
        raise ValueError("count must be non-negative and exposure positive")
    alpha = 1.0 - confidence
    if count == 0:
        return 0.0, -math.log(alpha / 2.0) / exposure_sessions
    z = NormalDist().inv_cdf(1.0 - alpha / 2.0)
    lower_count = count * (
        1.0 - 1.0 / (9.0 * count) - z / (3.0 * math.sqrt(count))
    ) ** 3
    upper_count = (count + 1) * (
        1.0 - 1.0 / (9.0 * (count + 1))
        + z / (3.0 * math.sqrt(count + 1))
    ) ** 3
    return max(0.0, lower_count / exposure_sessions), upper_count / exposure_sessions


def projection(
    *, count: int, elapsed_sessions: int, as_of_session: date,
) -> dict[str, Any]:
    remaining = max(0, TARGET_EPISODES - count)
    observed_rate = count / elapsed_sessions
    rate_low, rate_high = poisson_rate_interval(count, elapsed_sessions)
    stable = count >= MIN_STABLE_EPISODES and elapsed_sessions >= MIN_STABLE_SESSIONS
    result: dict[str, Any] = {
        "status": "STABLE_PLANNING_ESTIMATE" if stable else "UNSTABLE_INSUFFICIENT_SAMPLE",
        "planning_only": True,
        "changes_verdict_threshold": False,
        "constant_rate_assumption": (
            "Future non-overlapping clean labelable episodes accrue at the observed "
            "average rate, with no detector change, outage, seasonality, or regime shift."
        ),
        "method": "constant_rate_poisson_95pct_byar_approximation",
        "sample_floor": {
            "minimum_episodes": MIN_STABLE_EPISODES,
            "minimum_elapsed_trading_sessions": MIN_STABLE_SESSIONS,
        },
        "observed_rate_per_trading_session": round(observed_rate, 6),
        "planning_rate_interval_per_session": {
            "low": round(rate_low, 6), "high": round(rate_high, 6),
        },
    }
    if not stable:
        reasons = []
        if count < MIN_STABLE_EPISODES:
            reasons.append(f"episodes_below_{MIN_STABLE_EPISODES}")
        if elapsed_sessions < MIN_STABLE_SESSIONS:
            reasons.append(f"elapsed_sessions_below_{MIN_STABLE_SESSIONS}")
        result["insufficient_sample_reasons"] = reasons
    if remaining == 0:
        result.update({
            "status": "TARGET_REACHED",
            "additional_trading_sessions": 0,
            "projected_calendar_date": as_of_session.isoformat(),
        })
        return result
    fast_sessions = max(1, math.ceil(remaining / rate_high))
    slow_sessions = math.ceil(remaining / rate_low) if rate_low > 0 else None
    result["additional_trading_sessions_range"] = {
        "fast": fast_sessions, "slow": slow_sessions,
    }
    result["projected_calendar_date_range"] = {
        "earliest": advance_trading_sessions(as_of_session, fast_sessions).isoformat(),
        "latest": (
            advance_trading_sessions(as_of_session, slow_sessions).isoformat()
            if slow_sessions is not None else None
        ),
    }
    if stable and observed_rate > 0:
        point_sessions = math.ceil(remaining / observed_rate)
        result["constant_rate_point_estimate"] = {
            "additional_trading_sessions": point_sessions,
            "projected_calendar_date": advance_trading_sessions(
                as_of_session, point_sessions).isoformat(),
        }
    return result


def build_status(
    *, summary: dict[str, Any], labels: list[dict[str, Any]],
    summary_path: Path, labels_path: Path, as_of_session: date,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    if summary.get("endpoint_source") != store.DENSE_ENDPOINT_SOURCE:
        raise AccrualStatusError("dense_summary_provenance_required")
    if summary.get("data_integrity_status") != "PASS":
        raise AccrualStatusError(
            f"dense_summary_integrity_not_pass={summary.get('data_integrity_status')!r}")
    counted = count_non_overlapping(labels)
    summary_count = summary.get("clean_a2_labelable_episode_count")
    if summary_count != counted["raw_labelable_count"]:
        raise AccrualStatusError(
            "dense_summary_label_count_mismatch="
            f"summary:{summary_count!r},labels:{counted['raw_labelable_count']}"
        )
    sessions = trading_sessions(V1_2_ACCRUAL_START, as_of_session)
    if not sessions:
        raise AccrualStatusError("no_elapsed_trading_sessions_since_v1_2_start")
    kept = counted.pop("labels")
    label_sessions = Counter(str(row.get("session_date") or "") for row in kept)
    invalid_sessions = sorted(
        value for value in label_sessions
        if not value or date.fromisoformat(value) not in sessions
    )
    if invalid_sessions:
        raise AccrualStatusError(
            "label_sessions_outside_accrual_window=" + ",".join(invalid_sessions))
    non_overlap_count = counted["non_overlapping_count"]
    rate = non_overlap_count / len(sessions)
    gate = {
        "definition": "at_least_200_non_overlapping_clean_labelable_episodes",
        "target": TARGET_EPISODES,
        **counted,
        "remaining": max(0, TARGET_EPISODES - non_overlap_count),
        "reached": non_overlap_count >= TARGET_EPISODES,
        "non_overlap_rule": {
            "first_observation_per_episode_key": True,
            "scope": "symbol_and_side",
            "block_minutes": NON_OVERLAP_MINUTES,
        },
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "basis": "underlying_forward_return_a2_mvp_edge",
        "source": {
            "accrual_store": "dense_a2_v0",
            "endpoint_source": store.DENSE_ENDPOINT_SOURCE,
            "summary_path": str(summary_path),
            "labels_path": str(labels_path),
            "legacy_fallback_allowed": False,
        },
        "gate": gate,
        "accrual_window": {
            "v1_2_accrual_start": V1_2_ACCRUAL_START.isoformat(),
            "as_of_trading_session": as_of_session.isoformat(),
            "trading_calendar": "XNYS_US_EQUITY_HOLIDAY_CALENDAR_V0",
            "elapsed_trading_sessions": len(sessions),
            "sessions_with_labelable_episodes": len(label_sessions),
            "labelable_episode_counts_by_session": dict(sorted(label_sessions.items())),
        },
        "fire_rate": {
            "non_overlapping_episodes_per_trading_session": round(rate, 6),
            "non_overlapping_episodes_per_five_session_week": round(rate * 5.0, 6),
            "denominator": "all_elapsed_XNYS_sessions_since_v1_2_accrual_start",
        },
        "projection": projection(
            count=non_overlap_count, elapsed_sessions=len(sessions),
            as_of_session=as_of_session),
        "interpretation": (
            "Planning-only operational accrual estimate; never validated predictive "
            "evidence and never a change to the frozen 200-episode gate."
        ),
    }


def summary_text(report: dict[str, Any]) -> str:
    gate = report["gate"]
    window = report["accrual_window"]
    rate = report["fire_rate"]
    plan = report["projection"]
    lines = [
        f"A2 accrual: {gate['non_overlapping_count']} / {gate['target']} "
        "non-overlapping clean labelable episodes",
        f"Raw labelable: {gate['raw_labelable_count']}; "
        f"overlap/dedup gap: {gate['gap_count']}",
        f"Fire rate: {rate['non_overlapping_episodes_per_trading_session']:.3f} "
        "per trading session; "
        f"{rate['non_overlapping_episodes_per_five_session_week']:.3f} per 5-session week "
        f"across {window['elapsed_trading_sessions']} XNYS sessions",
        f"Projection: {plan['status']} (planning only; constant-rate assumption)",
    ]
    date_range = plan.get("projected_calendar_date_range")
    session_range = plan.get("additional_trading_sessions_range")
    if date_range and session_range:
        lines.append(
            "Planning range: "
            f"{session_range['fast']}-{session_range['slow']} additional trading sessions; "
            f"{date_range['earliest']} to {date_range['latest']}"
        )
    if plan.get("insufficient_sample_reasons"):
        lines.append(
            "Insufficient sample: " + ", ".join(plan["insufficient_sample_reasons"]))
    lines.append("Assumption: " + plan["constant_rate_assumption"])
    return "\n".join(lines)


def write_status(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=store.DENSE_SUMMARY_PATH)
    parser.add_argument("--labels", type=Path, default=store.DENSE_LABELS_PATH)
    parser.add_argument("--output", type=Path, default=store.DENSE_STATUS_PATH)
    parser.add_argument("--as-of-session", type=date.fromisoformat)
    args = parser.parse_args(argv)
    try:
        summary = store.load_dense_summary(args.summary)
        labels = store.load_dense_labels(args.labels)
        report = build_status(
            summary=summary, labels=labels,
            summary_path=args.summary, labels_path=args.labels,
            as_of_session=args.as_of_session or last_completed_session())
        write_status(report, args.output)
    except (store.DenseAccrualStoreError, AccrualStatusError, OSError, ValueError) as exc:
        print(f"A2 accrual unavailable: {exc}", file=sys.stderr)
        return 2
    print(summary_text(report))
    print(f"Status JSON: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
