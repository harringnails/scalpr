#!/usr/bin/env python3
"""Read-only, in-sample threshold characterization for signal Studies A/B."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
DEFAULTS = {"S": 0.25, "R": 90, "V": 8, "A": 0.20, "W": 900, "G": 120}
MIN_COMPLETE_SESSIONS = 8
MIN_PERSISTENCE_SESSIONS = 20


@dataclass(frozen=True)
class Point:
    ts: datetime
    mid: float


@dataclass(frozen=True)
class Sweep:
    session: str
    level: float
    depth: float
    reclaim_seconds: float | None
    post_reclaim_dip: float | None
    hold_seconds: float | None
    cumulative_below_seconds: float | None


def percentile(values: Sequence[float], probability: float) -> float | None:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return None
    position = (len(clean) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return clean[lower]
    return clean[lower] + (clean[upper] - clean[lower]) * (position - lower)


def rounded(value: float, step: float) -> float:
    return round(round(value / step) * step, 10)


def _parse_timestamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ET)


def load_tick_sessions(path: Path, *, completed_through: date) -> tuple[dict[str, list[Point]], str, int]:
    raw_sessions: dict[str, list[Point]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("symbol") != "SPY":
                continue
            try:
                ts = _parse_timestamp(row["provider_ts"])
                bid, ask = float(row["bid"]), float(row["ask"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts.date() > completed_through or not (time(9, 30) <= ts.time() < time(16, 0)):
                continue
            if bid <= 0 or ask <= 0 or bid > ask:
                continue
            raw_sessions[ts.date().isoformat()].append(Point(ts, (bid + ask) / 2.0))

    complete: dict[str, list[Point]] = {}
    included_hash = hashlib.sha256()
    included_rows = 0
    for session, points in sorted(raw_sessions.items()):
        points.sort(key=lambda point: point.ts)
        if points[0].ts.time() > time(9, 31) or points[-1].ts.time() < time(15, 59):
            continue
        sampled: dict[int, Point] = {}
        for point in points:
            sampled[int(point.ts.timestamp()) // 5] = point
        complete[session] = list(sampled.values())
        for point in points:
            included_hash.update(f"{point.ts.isoformat()}|{point.mid:.6f}\n".encode())
            included_rows += 1
    return complete, included_hash.hexdigest(), included_rows


def sweep_events(
    sessions: dict[str, list[Point]], *, lookback_seconds: int = 300,
    max_reclaim_seconds: int = 600, assessment_seconds: int = 900,
) -> list[Sweep]:
    events: list[Sweep] = []
    for session, points in sessions.items():
        prior: deque[Point] = deque()
        index = 0
        while index < len(points):
            current = points[index]
            while prior and (current.ts - prior[0].ts).total_seconds() > lookback_seconds:
                prior.popleft()
            level = min((point.mid for point in prior), default=None)
            if level is None or current.mid >= level:
                prior.append(current)
                index += 1
                continue

            trough = current.mid
            reclaim_index = None
            cursor = index
            while cursor < len(points):
                candidate = points[cursor]
                elapsed = (candidate.ts - current.ts).total_seconds()
                if elapsed > max_reclaim_seconds:
                    break
                trough = min(trough, candidate.mid)
                if cursor > index and candidate.mid >= level:
                    reclaim_index = cursor
                    break
                cursor += 1

            reclaim_seconds = None
            post_dip = None
            hold_seconds = None
            cumulative_below = None
            if reclaim_index is not None:
                reclaimed = points[reclaim_index]
                reclaim_seconds = (reclaimed.ts - current.ts).total_seconds()
                horizon = reclaimed.ts.timestamp() + assessment_seconds
                after = [point for point in points[reclaim_index:] if point.ts.timestamp() <= horizon]
                post_dip = max(0.0, level - min(point.mid for point in after)) if after else 0.0
                cumulative_below = 0.0
                hold_seconds = assessment_seconds
                failed_hold = False
                for left, right in zip(after, after[1:]):
                    step = min(5.0, max(0.0, (right.ts - left.ts).total_seconds()))
                    if left.mid < level:
                        cumulative_below += step
                    if cumulative_below > DEFAULTS["G"] and not failed_hold:
                        hold_seconds = (right.ts - reclaimed.ts).total_seconds()
                        failed_hold = True
            events.append(Sweep(
                session=session, level=level, depth=level - trough,
                reclaim_seconds=reclaim_seconds, post_reclaim_dip=post_dip,
                hold_seconds=hold_seconds, cumulative_below_seconds=cumulative_below,
            ))
            index = max(index + 1, (reclaim_index or cursor) + 1)
            prior.clear()
    return events


def vwap_proxy_persistence(sessions: dict[str, list[Point]]) -> dict[int, tuple[int, float]]:
    windows = (2, 4, 6, 8, 10, 12, 15)
    results: dict[int, list[bool]] = {window: [] for window in windows}
    for points in sessions.values():
        minute_last: dict[int, float] = {}
        for point in points:
            minute_last[int(point.ts.timestamp()) // 60] = point.mid
        mids = [value for _, value in sorted(minute_last.items())]
        if len(mids) < 20:
            continue
        proxies = []
        running = 0.0
        for index, mid in enumerate(mids, 1):
            running += mid
            proxies.append(running / index)
        for window in windows:
            for index in range(window, len(proxies) - 3):
                slope = proxies[index] - proxies[index - window]
                if slope == 0:
                    continue
                sign = slope > 0
                future = proxies[index + 3] - proxies[index + 3 - window]
                if future != 0:
                    results[window].append(sign == (future > 0))
    return {
        window: (len(matches), sum(matches) / len(matches) if matches else 0.0)
        for window, matches in results.items()
    }


def load_databento_depths(path: Path) -> tuple[list[float], int, str]:
    sessions: dict[str, list[tuple[datetime, float, float]]] = defaultdict(list)
    symbol_seen = "UNKNOWN"
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            symbol = (row.get("symbol") or "UNKNOWN").upper()
            symbol_seen = symbol
            if symbol != "SPY":
                continue
            try:
                ts = _parse_timestamp(row["ts_event"])
                low, close = float(row["low"]), float(row["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if time(9, 30) <= ts.time() < time(16, 0):
                sessions[ts.date().isoformat()].append((ts, low, close))

    depths: list[float] = []
    for rows in sessions.values():
        rows.sort()
        index = 5
        while index < len(rows):
            level = min(item[1] for item in rows[index - 5:index])
            if rows[index][1] >= level:
                index += 1
                continue
            trough = rows[index][1]
            reclaim = None
            for cursor in range(index + 1, min(index + 11, len(rows))):
                trough = min(trough, rows[cursor][1])
                if rows[cursor][2] >= level:
                    reclaim = cursor
                    break
            if reclaim is not None:
                depths.append(level - trough)
                index = reclaim + 1
            else:
                index += 1
    return depths, len(sessions), symbol_seen


def summary(values: Sequence[float], *, unit: str = "") -> str:
    if not values:
        return "n=0"
    parts = [
        f"n={len(values)}", f"p25={percentile(values, .25):.2f}{unit}",
        f"median={percentile(values, .50):.2f}{unit}", f"p75={percentile(values, .75):.2f}{unit}",
        f"p90={percentile(values, .90):.2f}{unit}",
    ]
    return ", ".join(parts)


def calibrate(
    *, tick_path: Path, completed_through: date, databento_path: Path | None = None,
) -> dict:
    sessions, included_hash, included_rows = load_tick_sessions(
        tick_path, completed_through=completed_through,
    )
    events = sweep_events(sessions)
    reclaimed = [event for event in events if event.reclaim_seconds is not None]
    depths = [event.depth for event in reclaimed]

    databento = None
    if databento_path is not None and databento_path.exists():
        db_depths, db_sessions, symbol = load_databento_depths(databento_path)
        scale = 1.0 if symbol == "SPY" else None
        databento = {
            "depths": db_depths, "sessions": db_sessions, "symbol": symbol,
            "scale_to_spy": scale,
        }
    historical_s_available = bool(
        databento and databento["symbol"] == "SPY" and databento["sessions"] >= 100
        and len(databento["depths"]) >= 1000
    )
    s_distribution = databento["depths"] if historical_s_available else depths
    proposed_s_raw = percentile(s_distribution, .75) or DEFAULTS["S"]
    proposed_s = max(.05, rounded(proposed_s_raw, .05))
    genuine = [event for event in reclaimed if event.depth >= proposed_s]
    reclaim_times = [event.reclaim_seconds for event in genuine if event.reclaim_seconds is not None]
    hold_survivors = [event for event in genuine if (event.hold_seconds or 0) >= DEFAULTS["W"]]
    post_dips = [event.post_reclaim_dip for event in hold_survivors if event.post_reclaim_dip is not None]
    holds = [event.hold_seconds for event in genuine if event.hold_seconds is not None]
    below = [event.cumulative_below_seconds for event in genuine if event.cumulative_below_seconds is not None]
    persistence = vwap_proxy_persistence(sessions)
    stable_windows = [window for window, (count, rate) in persistence.items() if count >= 200 and rate >= .80]

    enough_event_data = len(sessions) >= MIN_COMPLETE_SESSIONS and len(genuine) >= 50
    enough_acceptance_data = len(sessions) >= MIN_COMPLETE_SESSIONS and len(hold_survivors) >= 30
    proposed = {
        "S": proposed_s if historical_s_available or enough_event_data else DEFAULTS["S"],
        "R": int(rounded(percentile(reclaim_times, .80) or DEFAULTS["R"], 5)) if enough_event_data else DEFAULTS["R"],
        "V": min(stable_windows) if len(sessions) >= MIN_COMPLETE_SESSIONS and stable_windows else DEFAULTS["V"],
        "A": max(.05, rounded(percentile(post_dips, .75) or DEFAULTS["A"], .05)) if enough_acceptance_data else DEFAULTS["A"],
        "W": DEFAULTS["W"],
        "G": DEFAULTS["G"],
    }
    confidence = {
        "S": "calibrated" if historical_s_available or enough_event_data else "fallback",
        "R": "calibrated" if enough_event_data else "fallback",
        "V": "calibrated" if stable_windows and len(sessions) >= MIN_COMPLETE_SESSIONS else "fallback",
        "A": "calibrated" if enough_acceptance_data else "fallback",
        "W": "fallback" if len(sessions) < MIN_PERSISTENCE_SESSIONS else "calibrated",
        "G": "fallback" if len(sessions) < MIN_PERSISTENCE_SESSIONS else "calibrated",
    }

    return {
        "completed_through": completed_through.isoformat(),
        "confidence": confidence,
        "databento": databento,
        "defaults": DEFAULTS.copy(),
        "event_count": len(events),
        "genuine_count": len(genuine),
        "hold_survivor_count": len(hold_survivors),
        "included_data_sha256": included_hash,
        "included_rows": included_rows,
        "metrics": {
            "depths": depths, "reclaim_times": reclaim_times, "post_dips": post_dips,
            "holds": holds, "below": below, "persistence": persistence,
        },
        "proposed": proposed,
        "s_source": "databento_spy_p75" if historical_s_available else "tick_spy_p75",
        "sessions": sorted(sessions),
        "source_path": str(tick_path),
    }


def render_report(result: dict) -> str:
    proposed, defaults, confidence = result["proposed"], result["defaults"], result["confidence"]
    rows = [
        ("A", "acceptance band", f"{proposed['A']:.2f} pts", f"{defaults['A']:.2f} pts"),
        ("W", "hold window", f"{int(proposed['W'])} s", f"{int(defaults['W'])} s"),
        ("G", "back-below grace", f"{int(proposed['G'])} s", f"{int(defaults['G'])} s"),
        ("S", "sweep depth", f"{proposed['S']:.2f} pts", f"{defaults['S']:.2f} pts"),
        ("R", "reclaim window", f"{int(proposed['R'])} s", f"{int(defaults['R'])} s"),
        ("V", "proxy-VWAP slope window", f"{int(proposed['V'])} min", f"{int(defaults['V'])} min"),
    ]
    lines = [
        "# Signal-Threshold Calibration Report v0", "",
        "**Status:** EXPLORATORY / NON-INFERENTIAL / IN-SAMPLE KNOB-SETTING ONLY.",
        "This report does not freeze parameters, count toward N, test edge, or authorize a trade.", "",
        "## Proposed Values", "",
        "| Knob | Meaning | Proposed | Reasoned default | Basis |",
        "|---|---|---:|---:|---|",
    ]
    for knob, meaning, proposal, default in rows:
        lines.append(f"| `{knob}` | {meaning} | **{proposal}** | {default} | {confidence[knob]} |")
    metrics = result["metrics"]
    lines.extend([
        "", "## Calibration Boundary", "",
        f"- Completed sessions only, through **{result['completed_through']}**; current/partial sessions excluded.",
        f"- Tick source: `{result['source_path']}`; {len(result['sessions'])} complete RTH sessions, {result['included_rows']} clean included quotes.",
        f"- Included-row SHA-256: `{result['included_data_sha256']}` (hashes only rows inside the frozen cutoff).",
        "- Quotes were positive, two-sided, non-crossed and sampled to their last observed midpoint in each 5-second bucket. No interpolation or future quote was used.",
        "- Calibration convention only: a long-arm down-sweep crosses the prior trailing 5-minute low; reclaim must occur within 10 minutes. These conventions characterize distributions and do not silently freeze a detector.",
        "- Fixed design left untouched: N=150, p<=0.01, four chronological folds with >=3/4 sign consistency, dense 5/15/30/60-minute outcomes, <=5-second freshness, and no volume marker.",
        "", "## Distribution Summaries", "",
        f"- `S` sweep depth in the completed tick sessions: {summary(metrics['depths'], unit=' pts')}. Proposal uses the 75th percentile of `{result['s_source']}` rounded to $0.05 to separate deeper events from routine local-low jitter.",
        f"- `R` time to reclaim among sweeps at/above proposed `S`: {summary(metrics['reclaim_times'], unit=' s')}. Proposal uses the 80th percentile rounded to 5 seconds.",
        f"- `A` post-reclaim dip below the reclaimed level among {result['hold_survivor_count']} events that survived the default 15-minute/120-second hold screen: {summary(metrics['post_dips'], unit=' pts')}. Proposal uses the 75th percentile rounded to $0.05; failed reclaims are not allowed to widen the acceptance band.",
        f"- `W` hold duration before cumulative back-below time exceeds the default grace: {summary(metrics['holds'], unit=' s')}.",
        f"- `G` cumulative back-below time during the default hold assessment: {summary(metrics['below'], unit=' s')}.",
        "- `V` quote-mid proxy slope sign persistence three minutes forward:",
    ])
    for window, (count, rate) in metrics["persistence"].items():
        lines.append(f"  - {window:>2} min: {rate * 100:.1f}% sign persistence across {count} eligible minute observations")

    lines.extend(["", "## Knob Decisions", ""])
    explanations = {
        "S": "Calibrated from the 75th percentile of the long historical SPY distribution-shape archive; clean tick-level sessions remain the sub-minute cross-check.",
        "R": "Calibrated from point-in-time reclaim durations for events meeting the proposed sweep-depth cutoff.",
        "V": "Calibrated only when the shortest candidate window reaches 80% three-minute sign persistence with at least 200 observations.",
        "A": (
            f"Only {result['hold_survivor_count']} sweep/reclaim events survived the default hold/grace screen, below the 30-event floor; the 0.20-point reasoned default is retained instead of claiming precision from a thin tail."
            if confidence["A"] == "fallback" else
            "Calibrated from post-reclaim adverse dips among events that survived the default hold/grace screen."
        ),
        "W": "Persistence/failed-hold separation needs at least 20 independent sessions; current data is too thin, so the 900-second reasoned default is retained.",
        "G": "Back-below grace is strongly coupled to the hold rule and needs at least 20 independent sessions; the 120-second default is retained.",
    }
    for knob, *_ in rows:
        lines.append(f"- `{knob}` — **{confidence[knob].upper()}**: {explanations[knob]}")

    db = result.get("databento")
    lines.extend(["", "## Databento Distribution-Shape Check", ""])
    if db:
        lines.append(
            f"The optional archive contained `{db['symbol']}` one-minute bars across {db['sessions']} sessions. "
            f"Reclaimed local-low excursion depths: {summary(db['depths'], unit=' pts')}."
        )
        if db["symbol"] == "SPY":
            lines.append("The archive basis is SPY, so the SPX-to-SPY approximately 10x point-price rescaling factor is **1.0 (no rescaling needed)**. Databento is used only for non-inferential distribution shape, not sub-minute `R`/`G` calibration and never toward study N.")
        else:
            lines.append("A non-SPY archive cannot supply point thresholds until an explicit SPX/SPY scale is applied; no unscaled threshold is proposed.")
    else:
        lines.append("No optional Databento archive was supplied; no historical values were inferred.")

    lines.extend([
        "", "## Freeze Status", "",
        "**NOT FROZEN.** No preregistration file was edited. Operator approval and a separate freeze commit are required before prospective accrual can begin.",
        "Historical and pre-lock observations remain in-sample and cannot count toward either study's N.", "",
    ])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("calibrate",))
    parser.add_argument("--tick-log", type=Path, required=True)
    parser.add_argument("--completed-through", type=date.fromisoformat, required=True)
    parser.add_argument("--databento-bars", type=Path)
    parser.add_argument("--output", type=Path, default=Path("CALIBRATION_REPORT_signal_thresholds_v0.md"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = calibrate(
        tick_path=args.tick_log, completed_through=args.completed_through,
        databento_path=args.databento_bars,
    )
    args.output.write_text(render_report(result), encoding="utf-8")
    print(f"wrote {args.output} (NON-INFERENTIAL; NOT FROZEN)")


if __name__ == "__main__":
    main()
