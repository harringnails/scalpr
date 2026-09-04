#!/usr/bin/env python3
"""Append-only Study B evaluator for the frozen intraday continuation sequence."""

from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path
from typing import Any

import signal_episode_common_v0 as common


STUDY_ID = "intraday_continuation_v0"
SWEEP_DEPTH_POINTS = 0.25
RECLAIM_WINDOW_SECONDS = 180
PROXY_SLOPE_WINDOW_MINUTES = 2
POSITIVE_MINUTE_COUNT = 3
DEFAULT_OUTPUT = Path("intraday_continuation_v0.jsonl")
DEFAULT_PREREG = Path("PREREG_intraday_continuation_v0.md")


def excluded(record: dict[str, Any], reason: str, **fields: Any) -> dict[str, Any]:
    return common.finalize_record({
        **record, **fields, "counts_toward_n": False, "episode_status": "EXCLUDED",
        "exclusion_reason": reason,
    })


def sweep_reclaims(quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not quotes:
        return events
    local_low = quotes[0]["mid"]
    index = 1
    while index < len(quotes):
        quote = quotes[index]
        if quote["mid"] > local_low - SWEEP_DEPTH_POINTS:
            local_low = min(local_low, quote["mid"])
            index += 1
            continue
        level = local_low
        reclaim = None
        cursor = index + 1
        while cursor < len(quotes):
            candidate = quotes[cursor]
            elapsed = (candidate["provider_ts"] - quote["provider_ts"]).total_seconds()
            if elapsed > RECLAIM_WINDOW_SECONDS:
                break
            if candidate["mid"] >= level:
                reclaim = candidate
                break
            cursor += 1
        if reclaim is not None:
            events.append({"level": level, "reclaim": reclaim, "sweep": quote})
            index = cursor + 1
        else:
            local_low = min(local_low, quote["mid"])
            index += 1
    return events


def minute_closes(quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_minute: dict[int, dict[str, Any]] = {}
    for quote in quotes:
        by_minute[int(quote["provider_ts"].timestamp()) // 60] = quote
    closes = [by_minute[key] for key in sorted(by_minute)]
    running = 0.0
    for index, quote in enumerate(closes, 1):
        running += quote["mid"]
        quote["proxy_vwap"] = running / index
    return closes


def acceptance_marker(closes: list[dict[str, Any]], reclaim_ts: Any) -> tuple[int, dict[str, Any]] | None:
    consecutive = 0
    for index in range(1, len(closes)):
        if closes[index]["provider_ts"] <= reclaim_ts:
            continue
        if closes[index]["mid"] > closes[index - 1]["mid"]:
            consecutive += 1
        else:
            consecutive = 0
        if consecutive >= POSITIVE_MINUTE_COUNT:
            return index, closes[index]
    return None


def proxy_vwap_marker(
    closes: list[dict[str, Any]], acceptance_index: int,
) -> tuple[int, dict[str, Any]] | None:
    for index in range(max(acceptance_index, PROXY_SLOPE_WINDOW_MINUTES), len(closes)):
        quote = closes[index]
        earlier = closes[index - PROXY_SLOPE_WINDOW_MINUTES]
        if quote["mid"] >= quote["proxy_vwap"] and quote["proxy_vwap"] > earlier["proxy_vwap"]:
            return index, quote
    return None


def first_wall_cross_after(
    quotes: list[dict[str, Any]], *, after: Any, flash_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], float, dict[str, Any]] | None:
    previous = None
    for quote in quotes:
        if quote["provider_ts"] < after:
            previous = quote
            continue
        wall_source = next((
            row for row in reversed(flash_rows)
            if row["effective_ts"] <= quote["provider_ts"]
            and common.flash_values(row)["call_wall"] is not None
        ), None)
        if wall_source is None:
            previous = quote
            continue
        wall = common.flash_values(wall_source)["call_wall"]
        if previous is not None and previous["mid"] < wall <= quote["mid"]:
            return quote, wall, wall_source
        previous = quote
    return None


def evaluate_session(
    *, session_date: str, quotes: list[dict[str, Any]],
    flash_rows: list[dict[str, Any]], prereg_path: Path,
) -> dict[str, Any]:
    record = common.base_record(study_id=STUDY_ID, session_date=session_date, prereg_path=prereg_path)
    record["frozen_parameters"] = {
        "N": 150,
        "R_seconds": RECLAIM_WINDOW_SECONDS,
        "S_points": SWEEP_DEPTH_POINTS,
        "V_minutes": PROXY_SLOPE_WINDOW_MINUTES,
        "acceptance_positive_minutes": POSITIVE_MINUTE_COUNT,
        "p_max": 0.01,
        "walk_forward": "4_fold_chronological_sign_consistent_at_least_3_of_4",
    }
    record["proxy_vwap_basis"] = "quote_mid_proxy_vwap_not_traded_vwap"
    if common.prelock(session_date):
        return excluded(record, "PRELOCK_IN_SAMPLE")
    if not quotes:
        return excluded(record, "MISSING_SESSION_QUOTES")

    open_at, close_at = common.session_bounds(session_date)
    rth_quotes = [quote for quote in quotes if open_at <= quote["provider_ts"] < close_at]
    if not rth_quotes:
        return excluded(record, "MISSING_RTH_QUOTES")
    closes = minute_closes(rth_quotes)
    all_session_flash = [
        row for row in flash_rows
        if row.get("session_date") == session_date and open_at <= row["effective_ts"] < close_at
    ]
    session_flash = [row for row in all_session_flash if common.flash_is_fresh(row)]
    input_provenance = {
        "rth_quote_range": {
            "first": common.quote_provenance(rth_quotes[0], rth_quotes[0]["provider_ts"]),
            "last": common.quote_provenance(rth_quotes[-1], rth_quotes[-1]["provider_ts"]),
            "records": len(rth_quotes),
        },
        "flashalpha_range": {
            "first": common.flash_provenance(all_session_flash[0]) if all_session_flash else {"status": "MISSING"},
            "fresh_records": len(session_flash),
            "last": common.flash_provenance(all_session_flash[-1]) if all_session_flash else {"status": "MISSING"},
            "records": len(all_session_flash),
        },
    }
    if not session_flash:
        return excluded(record, "MISSING_OR_STALE_FLASHALPHA_SESSION", provenance=input_provenance)

    last_failure = "NO_SWEEP_RECLAIM_SEQUENCE"
    for event in sweep_reclaims(rth_quotes):
        sweep, reclaim, level = event["sweep"], event["reclaim"], event["level"]
        marker = acceptance_marker(closes, reclaim["provider_ts"])
        if marker is None:
            last_failure = "NO_THREE_POSITIVE_MINUTE_ACCEPTANCE"
            continue
        acceptance_index, acceptance = marker
        proxy_marker = proxy_vwap_marker(closes, acceptance_index)
        if proxy_marker is None:
            last_failure = "NO_PROXY_VWAP_RECLAIM_WITH_POSITIVE_SLOPE"
            continue
        _, proxy_quote = proxy_marker
        wall_cross = first_wall_cross_after(
            rth_quotes, after=proxy_quote["provider_ts"], flash_rows=session_flash,
        )
        if wall_cross is None:
            last_failure = "NO_CALL_WALL_CROSS_AFTER_PROXY_MARKER"
            continue
        cross, wall, wall_source = wall_cross
        next_reads = [row for row in session_flash if row["effective_ts"] > cross["provider_ts"]]
        if not next_reads:
            last_failure = "MISSING_NEXT_FLASHALPHA_READ"
            continue
        migration = next_reads[0]
        migrated_wall = common.flash_values(migration)["call_wall"]
        if migrated_wall is None or migrated_wall <= wall:
            last_failure = "NEXT_READ_HAS_NO_UPWARD_WALL_MIGRATION"
            continue

        t0 = migration["effective_ts"]
        provenance = {
            "acceptance": {
                **common.quote_provenance(acceptance, acceptance["provider_ts"]),
                "positive_minute_count": POSITIVE_MINUTE_COUNT,
            },
            "inputs": input_provenance,
            "migration_confirmation": common.flash_provenance(migration),
            "proxy_vwap": {
                **common.quote_provenance(proxy_quote, proxy_quote["provider_ts"]),
                "basis": "quote_mid_proxy_vwap_not_traded_vwap",
                "proxy_value": round(proxy_quote["proxy_vwap"], 6),
                "slope_window_minutes": PROXY_SLOPE_WINDOW_MINUTES,
            },
            "reclaim": common.quote_provenance(reclaim, reclaim["provider_ts"]),
            "sweep": {
                **common.quote_provenance(sweep, sweep["provider_ts"]),
                "depth_points": round(level - sweep["mid"], 6),
                "local_extreme": round(level, 6),
            },
            "wall_cross": {
                **common.quote_provenance(cross, cross["provider_ts"]),
                "call_wall": wall,
                "wall_source": common.flash_provenance(wall_source),
            },
            "wall_migration": {"from": wall, "to": migrated_wall},
        }
        outcome = common.label_outcome(rth_quotes, t0)
        provenance["outcome"] = outcome["provenance"]
        if outcome["a2_outcome_status"] != "A2-AVAILABLE":
            return excluded(
                record, "A2_UNAVAILABLE_MISSING_POINT", anchor_t0_utc=t0.isoformat(),
                outcome=outcome, provenance=provenance,
            )
        return common.finalize_record({
            **record,
            "anchor_t0_utc": t0.isoformat(),
            "counts_toward_n": True,
            "episode_status": "A2-AVAILABLE",
            "exclusion_reason": None,
            "outcome": outcome,
            "provenance": provenance,
        })
    return excluded(record, last_failure, provenance=input_provenance)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("evaluate",))
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--tick-log", type=Path, default=Path("tick_log.csv"))
    parser.add_argument("--flashalpha-ledger", type=Path, required=True)
    parser.add_argument("--prereg", type=Path, default=DEFAULT_PREREG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    record = evaluate_session(
        session_date=args.session_date,
        quotes=common.load_session_quotes(args.tick_log, args.session_date, include_premarket=False),
        flash_rows=common.load_flash_candidates(args.flashalpha_ledger),
        prereg_path=args.prereg,
    )
    written = common.append_once(args.output, record)
    print({
        "counts_toward_n": record["counts_toward_n"],
        "episode_status": record["episode_status"],
        "record_hash": record["record_hash"],
        "written": written,
    })


if __name__ == "__main__":
    main()
