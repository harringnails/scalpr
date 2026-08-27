"""Point-in-time A2 underlying-forward-return measurement pipeline.

This is a paper/shadow research labeler.  It consumes the admitted v1 episode
ledger and caller-supplied point-in-time SPY quotes; it never imports broker,
order, or Guard code.  The primary label is direction-adjusted 60-minute log
return.  The legacy tick log remains available only as an explicit cross-check.

The existing ``entry_policy.compute_decision_outcome`` helper is deliberately
not used for primary A2 labels: it works from completed minute bars, which can
contain quotes after an intraminute decision or horizon boundary.  A2 instead
uses provider event timestamps directly so the label remains point-in-time.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import entry_episode_integrity_v1 as episode_integrity


SCHEMA_VERSION = "a2-underlying-forward-return-v3"
DEFAULT_TICK_LOG = Path("tick_log.csv")
DEFAULT_EPISODES = Path("entry_intelligence_episodes_v1.jsonl")
DEFAULT_QUARANTINE_MANIFEST = episode_integrity.DEFAULT_QUARANTINE_MANIFEST
DEFAULT_OUTPUT_DIR = Path("v2_data") / "a2_measurement"
DEFAULT_OUTPUT_PATH = DEFAULT_OUTPUT_DIR / "a2_labels_v2.jsonl"
DEFAULT_SUMMARY_PATH = DEFAULT_OUTPUT_DIR / "a2_summary_v2.json"
HORIZONS_MIN = (5, 15, 30, 60)
ET = ZoneInfo("America/New_York")
LEGACY_ENDPOINT_SOURCE = "live_tick_log"
DENSE_ENDPOINT_SOURCE = "alpaca_historical_stock_quote_v1"
SHIPPED_CLOCK_SKEW_TOLERANCE_SECONDS = 3.0

# The logger samples at two seconds.  Five seconds is a precommitted maximum
# observation offset, not a relaxation of any option-quote freshness rule.
MAX_POINT_OFFSET_SECONDS = 5.0
BASE_MEASUREMENT_CONFIG = {
    "measurement_config_version": "a2-underlying-return-point-in-time-v2",
    "symbol": "SPY",
    "price": "two_sided_non_crossed_quote_mid",
    "event_timestamp": "provider_ts",
    "anchor_rule": "latest_provider_quote_at_or_before_t0_within_5s",
    "endpoint_rule": "latest_provider_quote_at_or_before_t0_plus_h_within_5s",
    "max_point_offset_seconds": MAX_POINT_OFFSET_SECONDS,
    "clock_skew_tolerance_seconds": SHIPPED_CLOCK_SKEW_TOLERANCE_SECONDS,
    "missing_endpoint_rule": "UNAVAILABLE_NEVER_IMPUTE",
    "return_convention": "natural_log_return",
    "horizons_minutes": list(HORIZONS_MIN),
}


def measurement_config(endpoint_source: str) -> dict[str, Any]:
    return {**BASE_MEASUREMENT_CONFIG, "endpoint_source": endpoint_source}


MEASUREMENT_CONFIG = measurement_config(LEGACY_ENDPOINT_SOURCE)
MEASUREMENT_CONFIG_HASH = hashlib.sha256(
    json.dumps(MEASUREMENT_CONFIG, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _session_bounds(session_date: str) -> tuple[datetime, datetime]:
    date_obj = datetime.fromisoformat(session_date).date()
    open_et = datetime.combine(date_obj, dtime(9, 30), tzinfo=ET)
    close_et = datetime.combine(date_obj, dtime(16, 0), tzinfo=ET)
    return open_et.astimezone(timezone.utc), close_et.astimezone(timezone.utc)


def _session_minute(decided_at: datetime, session_date: str) -> int:
    open_at, _ = _session_bounds(session_date)
    return int((decided_at - open_at).total_seconds() // 60)


def _clean_quote(row: dict[str, Any]) -> dict[str, Any] | None:
    provider_ts = _parse_dt(row.get("provider_ts"))
    if provider_ts is None:
        # Receipt timestamps are not interchangeable with provider event time.
        return None
    try:
        bid = float(row.get("bid") or 0)
        ask = float(row.get("ask") or 0)
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or bid > ask:
        return None
    return {
        "provider_ts": provider_ts,
        "received_at": _parse_dt(row.get("utc_time")),
        "mid": (bid + ask) / 2.0,
        "bid": bid,
        "ask": ask,
        "source": LEGACY_ENDPOINT_SOURCE,
        "endpoint_source": LEGACY_ENDPOINT_SOURCE,
    }


def load_tick_sessions(
    path: Path = DEFAULT_TICK_LOG, symbol: str = "SPY"
) -> dict[str, list[dict[str, Any]]]:
    """Load timestamped, clean SPY quotes grouped by RTH session date."""
    sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not path.exists():
        return {}
    with path.open(newline="") as source:
        for row in csv.DictReader(source):
            if str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            quote = _clean_quote(row)
            if quote is None:
                continue
            event_et = quote["provider_ts"].astimezone(ET)
            if event_et.weekday() >= 5:
                continue
            session_date = event_et.date().isoformat()
            open_at, close_at = _session_bounds(session_date)
            if open_at <= quote["provider_ts"] < close_at:
                sessions[session_date].append(quote)
    for quotes in sessions.values():
        quotes.sort(key=lambda quote: quote["provider_ts"])
    return dict(sessions)


def load_episodes(
    path: Path = DEFAULT_EPISODES,
    *,
    admitted_only: bool = True,
    quarantine_path: Path | None = None,
    exclude_quarantined: bool = True,
) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    if not path.exists():
        return episodes
    quarantine_path = quarantine_path or path.with_name(DEFAULT_QUARANTINE_MANIFEST.name)
    quarantined = (
        episode_integrity.quarantined_episode_record_ids(quarantine_path)
        if exclude_quarantined else set()
    )
    with path.open() as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            if admitted_only and not row.get("admitted"):
                continue
            if str(row.get("episode_record_id") or "") in quarantined:
                continue
            episodes.append(row)
    return sorted(
        episodes,
        key=lambda row: (row.get("session_date", ""), row.get("decided_at", ""), row.get("episode_key", "")),
    )


def deduplicate_episodes(episodes: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Retain only the first admitted row per immutable episode key."""
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for episode in episodes:
        key = str(episode.get("episode_key") or "")
        if not key or key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(episode)
    return unique, duplicates


def cross_side_timestamp_collisions(episodes: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Expose co-timed opposing records that cannot count as independent evidence."""
    sides_by_timestamp: dict[str, set[str]] = defaultdict(set)
    for episode in episodes:
        timestamp = str(episode.get("decided_at") or "")
        side = str(episode.get("side") or "").upper()
        if timestamp and side:
            sides_by_timestamp[timestamp].add(side)
    return {
        timestamp: sorted(sides)
        for timestamp, sides in sides_by_timestamp.items()
        if len(sides) > 1
    }


def clean_a2_episodes(
    episodes: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, dict[str, list[str]], int]:
    """Keep exactly the episodes eligible for A2 accrual.

    Quarantine filtering happens before this function.  A co-timed opposing
    pair is excluded as a group, not counted as two independent observations.
    """
    unique, duplicate_count = deduplicate_episodes(episodes)
    collisions = cross_side_timestamp_collisions(unique)
    collision_timestamps = set(collisions)
    clean = [
        episode for episode in unique
        if str(episode.get("decided_at") or "") not in collision_timestamps
    ]
    return clean, duplicate_count, collisions, len(unique) - len(clean)


def _latest_at_or_before(quotes: list[dict[str, Any]], target: datetime) -> dict[str, Any] | None:
    selected = None
    for quote in quotes:
        if quote["provider_ts"] > target:
            break
        selected = quote
    if selected is None:
        return None
    if (target - selected["provider_ts"]).total_seconds() > MAX_POINT_OFFSET_SECONDS:
        return None
    return selected


def _quote_stamp(
    quote: dict[str, Any] | None,
    target: datetime,
    endpoint_source: str,
) -> tuple[str | None, str, float | None, float | None]:
    if quote is None:
        return None, endpoint_source, None, None
    age = (target - quote["provider_ts"]).total_seconds()
    return (
        quote["provider_ts"].isoformat(),
        str(quote.get("endpoint_source") or quote.get("source") or endpoint_source),
        round(age, 6),
        round(-age, 6),
    )


def _returns_and_path(
    anchor: dict[str, Any], endpoints: dict[int, dict[str, Any] | None],
    quotes: list[dict[str, Any]], *, path_quotes_complete: bool,
) -> tuple[dict[str, float | None], float | None, float | None]:
    returns = {
        f"return_{horizon}m": (
            round(math.log(endpoint["mid"] / anchor["mid"]), 6) if endpoint else None
        )
        for horizon, endpoint in endpoints.items()
    }
    end_60 = endpoints.get(60)
    if end_60 is None or not path_quotes_complete:
        return returns, None, None
    path_mids = [
        quote["mid"]
        for quote in quotes
        if anchor["provider_ts"] < quote["provider_ts"] <= end_60["provider_ts"]
    ]
    if not path_mids:
        return returns, None, None
    return (
        returns,
        round(math.log(max(path_mids) / anchor["mid"]), 6),
        round(math.log(min(path_mids) / anchor["mid"]), 6),
    )


def _base_label(
    episode: dict[str, Any], decided_at: datetime | None, session_date: str,
    endpoint_source: str,
) -> dict[str, Any]:
    side = str(episode.get("side") or "").upper()
    direction_sign = 1 if side == "CALL" else -1 if side == "PUT" else None
    config = measurement_config(endpoint_source)
    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    endpoint_keys = [f"{horizon}m" for horizon in HORIZONS_MIN]
    return {
        "schema_version": SCHEMA_VERSION,
        "measurement_config_version": config["measurement_config_version"],
        "measurement_config_hash": config_hash,
        "config_version": episode.get("config_version"),
        "config_hash": episode.get("config_hash"),
        "episode_key": episode.get("episode_key"),
        "episode_record_id": episode.get("episode_record_id"),
        "decision_id": episode.get("decision_id"),
        "cohort_id": episode.get("cohort_id"),
        "symbol": episode.get("symbol"),
        "side": side,
        "setup_direction_sign": direction_sign,
        "direction_sign": direction_sign,
        "session_date": session_date or episode.get("session_date"),
        "decided_at": decided_at.isoformat() if decided_at else episode.get("decided_at"),
        "regime_tag": episode.get("regime_tag"),
        "is_calibrated_probability": False,
        "horizons_minutes": list(HORIZONS_MIN),
        "primary_metric": "signed_return_60m",
        "endpoint_source": endpoint_source,
        "source_store": (
            "alpaca_historical_stock_quotes:SIP"
            if endpoint_source == DENSE_ENDPOINT_SOURCE else "tick_log.csv"
        ),
        "source_timestamp_field": "provider_ts",
        "max_point_offset_seconds": MAX_POINT_OFFSET_SECONDS,
        "clock_skew_tolerance_seconds": SHIPPED_CLOCK_SKEW_TOLERANCE_SECONDS,
        "anchor_price": None,
        "anchor_provider_ts": None,
        "anchor_source": endpoint_source,
        "anchor_age_seconds": None,
        "anchor_offset_seconds": None,
        "endpoint_provider_ts": {key: None for key in endpoint_keys},
        "endpoint_sources": {key: endpoint_source for key in endpoint_keys},
        "endpoint_age_seconds": {key: None for key in endpoint_keys},
        "endpoint_offset_seconds": {key: None for key in endpoint_keys},
    }


def _finalize_label_id(label: dict[str, Any]) -> None:
    material = {
        "episode_key": label.get("episode_key"),
        "measurement_config_hash": label.get("measurement_config_hash"),
        "anchor_provider_ts": label.get("anchor_provider_ts"),
        "endpoint_provider_ts": label.get("endpoint_provider_ts"),
    }
    label["label_record_id"] = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def label_episode(
    episode: dict[str, Any], *, session_quotes: list[dict[str, Any]] | None,
    endpoint_source: str | None = None, path_quotes_complete: bool = True,
) -> dict[str, Any]:
    """Create one strictly point-in-time A2 label from an admitted episode."""
    decided_at = _parse_dt(episode.get("decided_at"))
    session_date = str(episode.get("session_date") or "")
    quotes = session_quotes or []
    resolved_source = endpoint_source or next(
        (str(row.get("endpoint_source") or row.get("source")) for row in quotes
         if row.get("endpoint_source") or row.get("source")),
        LEGACY_ENDPOINT_SOURCE,
    )
    label = _base_label(episode, decided_at, session_date, resolved_source)
    if decided_at is None or not session_date:
        label.update({
            "label_status": "UNAVAILABLE", "a2_outcome_status": "A2-UNAVAILABLE",
            "missing_reason": "missing_decision_time",
        })
        _finalize_label_id(label)
        return label
    if label["setup_direction_sign"] is None:
        label.update({
            "label_status": "UNAVAILABLE", "a2_outcome_status": "A2-UNAVAILABLE",
            "missing_reason": "unknown_setup_direction",
        })
        _finalize_label_id(label)
        return label

    anchor = _latest_at_or_before(quotes, decided_at)
    label["session_minute"] = _session_minute(decided_at, session_date)
    anchor_ts, anchor_source, anchor_age, anchor_offset = _quote_stamp(
        anchor, decided_at, resolved_source)
    label.update({
        "anchor_price": round(anchor["mid"], 6) if anchor else None,
        "anchor_provider_ts": anchor_ts,
        "anchor_source": anchor_source,
        "anchor_age_seconds": anchor_age,
        "anchor_offset_seconds": anchor_offset,
    })
    if anchor is None:
        label.update({
            "label_status": "UNAVAILABLE", "a2_outcome_status": "A2-UNAVAILABLE",
            "missing_reason": "missing_clean_anchor_within_5s",
        })
        _finalize_label_id(label)
        return label

    endpoints: dict[int, dict[str, Any] | None] = {}
    endpoint_ts: dict[str, str | None] = {}
    endpoint_sources: dict[str, str | None] = {}
    endpoint_ages: dict[str, float | None] = {}
    endpoint_offsets: dict[str, float | None] = {}
    missing: list[str] = []
    for horizon in HORIZONS_MIN:
        target = decided_at + timedelta(minutes=horizon)
        endpoint = _latest_at_or_before(quotes, target)
        endpoints[horizon] = endpoint
        stamp, source, age, offset = _quote_stamp(endpoint, target, resolved_source)
        key = f"{horizon}m"
        endpoint_ts[key] = stamp
        endpoint_sources[key] = source
        endpoint_ages[key] = age
        endpoint_offsets[key] = offset
        if endpoint is None:
            missing.append(f"missing_endpoint_{key}_within_5s")

    returns, mfe, mae = _returns_and_path(
        anchor, endpoints, quotes, path_quotes_complete=path_quotes_complete)
    signed = {
        f"signed_return_{horizon}m": (
            round(returns[f"return_{horizon}m"] * label["setup_direction_sign"], 6)
            if returns[f"return_{horizon}m"] is not None else None
        )
        for horizon in HORIZONS_MIN
    }
    label.update({
        "endpoint_provider_ts": endpoint_ts,
        "endpoint_sources": endpoint_sources,
        "endpoint_age_seconds": endpoint_ages,
        "endpoint_offset_seconds": endpoint_offsets,
        "path_quote_coverage": "COMPLETE" if path_quotes_complete else "ENDPOINT_WINDOWS_ONLY",
        "outcome": {"available": not missing, **returns, "mfe": mfe, "mae": mae},
        **returns,
        **signed,
        "mfe": mfe,
        "mae": mae,
        "primary_metric_value": signed["signed_return_60m"],
        "label_status": "AVAILABLE" if not missing else "UNAVAILABLE",
        "a2_outcome_status": "A2-AVAILABLE" if not missing else "A2-UNAVAILABLE",
        "missing_reason": None if not missing else ";".join(missing),
    })
    _finalize_label_id(label)
    return label


def measure_a2(
    *, episodes_path: Path = DEFAULT_EPISODES, tick_log_path: Path = DEFAULT_TICK_LOG,
    admitted_only: bool = True, quarantine_path: Path | None = None,
    session_date: str | None = None,
    quote_sessions: dict[str, list[dict[str, Any]]] | None = None,
    endpoint_source: str = LEGACY_ENDPOINT_SOURCE,
    path_quotes_complete: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    quarantine_path = quarantine_path or episodes_path.with_name(DEFAULT_QUARANTINE_MANIFEST.name)
    unfiltered = load_episodes(
        episodes_path, admitted_only=admitted_only,
        quarantine_path=quarantine_path, exclude_quarantined=False)
    episodes = load_episodes(
        episodes_path, admitted_only=admitted_only,
        quarantine_path=quarantine_path, exclude_quarantined=True)
    if session_date:
        unfiltered = [
            episode for episode in unfiltered
            if str(episode.get("session_date") or "") == session_date
        ]
        episodes = [
            episode for episode in episodes
            if str(episode.get("session_date") or "") == session_date
        ]
    quarantined_count = len(unfiltered) - len(episodes)
    clean_episodes, duplicate_count, collisions, collision_rows_excluded = clean_a2_episodes(episodes)
    sessions = (
        load_tick_sessions(tick_log_path)
        if quote_sessions is None and endpoint_source == LEGACY_ENDPOINT_SOURCE
        else (quote_sessions or {})
    )
    labeled = [
        label_episode(
            episode,
            session_quotes=sessions.get(str(episode.get("session_date") or "")),
            endpoint_source=endpoint_source,
            path_quotes_complete=path_quotes_complete,
        )
        for episode in clean_episodes
    ]
    return labeled, summarize_a2(
        labeled, episodes=episodes, sessions=sessions,
        duplicate_episode_rows_excluded=duplicate_count,
        quarantined_episode_rows_excluded=quarantined_count,
        clean_a2_eligible_episode_count=len(clean_episodes),
        collision_episode_rows_excluded=collision_rows_excluded,
        known_collisions=collisions,
        endpoint_source=endpoint_source,
    )


def summarize_a2(
    labeled: list[dict[str, Any]], *, episodes: list[dict[str, Any]] | None = None,
    sessions: dict[str, list[dict[str, Any]]] | None = None,
    duplicate_episode_rows_excluded: int = 0,
    quarantined_episode_rows_excluded: int = 0,
    clean_a2_eligible_episode_count: int | None = None,
    collision_episode_rows_excluded: int | None = None,
    known_collisions: dict[str, list[str]] | None = None,
    endpoint_source: str = LEGACY_ENDPOINT_SOURCE,
) -> dict[str, Any]:
    episodes = episodes or []
    sessions = sessions or {}
    available = [row for row in labeled if row.get("label_status") == "AVAILABLE"]
    missing = [row for row in labeled if row.get("label_status") != "AVAILABLE"]
    signed_60 = [row["signed_return_60m"] for row in available if row.get("signed_return_60m") is not None]
    collisions = known_collisions if known_collisions is not None else cross_side_timestamp_collisions(episodes)
    if collision_episode_rows_excluded is None:
        collision_timestamps = set(collisions)
        collision_episode_rows_excluded = sum(
            str(episode.get("decided_at") or "") in collision_timestamps
            for episode in episodes
        )
    if clean_a2_eligible_episode_count is None:
        clean_a2_eligible_episode_count = len(labeled)
    data_integrity_status = "PASS" if not collisions else "FAIL_CROSS_SIDE_TIMESTAMP_COLLISIONS"
    config = measurement_config(endpoint_source)
    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "measurement_config_version": config["measurement_config_version"],
        "measurement_config_hash": config_hash,
        "endpoint_source": endpoint_source,
        "n_episodes_input": len(episodes),
        "n_quarantined_episode_rows_excluded": quarantined_episode_rows_excluded,
        "n_duplicate_episode_rows_excluded": duplicate_episode_rows_excluded,
        "n_collision_episode_rows_excluded": collision_episode_rows_excluded,
        "clean_a2_eligible_episode_count": clean_a2_eligible_episode_count,
        "clean_a2_labelable_episode_count": len(available),
        "clean_a2_unavailable_episode_count": len(missing),
        "n_labeled": len(labeled),
        "n_available": len(available),
        "n_missing": len(missing),
        "n_cross_side_timestamp_collision_groups": len(collisions),
        "cross_side_timestamp_collisions": collisions,
        "data_integrity_status": data_integrity_status,
        "n_sessions_with_clean_quotes": len(sessions),
        "sessions_with_clean_quotes": sorted(sessions),
        "side_counts": dict(Counter(str(row.get("side") or "UNKNOWN") for row in labeled)),
        "label_status_counts": dict(Counter(str(row.get("label_status") or "UNKNOWN") for row in labeled)),
        "missing_reason_counts": dict(Counter(str(row.get("missing_reason") or "none") for row in missing)),
        "mean_signed_return_60m": round(mean(signed_60), 6) if signed_60 else None,
        "min_signed_return_60m": round(min(signed_60), 6) if signed_60 else None,
        "max_signed_return_60m": round(max(signed_60), 6) if signed_60 else None,
        "power_gate_reached": len(available) >= 200 and data_integrity_status == "PASS",
        "phase4_preflight": (
            "READY_FOR_PHASE4" if len(available) >= 200 and data_integrity_status == "PASS"
            else "BLOCKED_BY_CROSS_SIDE_COLLISIONS" if collisions
            else "UNDERPOWERED_INCONCLUSIVE"
        ),
        "notes": (
            "A2 labels use provider-time two-sided SPY mids. Anchor quotes must be at or before t0; "
            "endpoints use the last fresh quote at or before each fixed horizon. Missing points remain unavailable. "
            "Only admitted, non-quarantined, non-collision episodes accrue; option trackability is irrelevant. "
            "Cross-side co-timed records are a Phase-4 integrity failure, not independent evidence."
        ),
    }


def append_jsonl(rows: Iterable[dict[str, Any]], path: Path) -> int:
    """Append new immutable labels; never overwrite an earlier evidence row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if path.exists():
        with path.open() as source:
            for line in source:
                if line.strip():
                    existing.add(str(json.loads(line).get("label_record_id") or ""))
    appended = 0
    with path.open("a") as target:
        for row in rows:
            record_id = str(row.get("label_record_id") or "")
            if not record_id or record_id in existing:
                continue
            target.write(json.dumps(row, sort_keys=True) + "\n")
            existing.add(record_id)
            appended += 1
    return appended


def write_summary(summary: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as target:
        json.dump(summary, target, indent=2, sort_keys=True)


def materialize_a2(
    *, episodes_path: Path = DEFAULT_EPISODES, tick_log_path: Path = DEFAULT_TICK_LOG,
    quarantine_path: Path | None = None, output_path: Path = DEFAULT_OUTPUT_PATH,
    summary_path: Path = DEFAULT_SUMMARY_PATH,
    quote_sessions: dict[str, list[dict[str, Any]]] | None = None,
    endpoint_source: str = LEGACY_ENDPOINT_SOURCE,
    path_quotes_complete: bool = True,
) -> dict[str, Any]:
    """Append idempotent research labels and refresh the aggregate A2 summary."""
    labeled, summary = measure_a2(
        episodes_path=episodes_path, tick_log_path=tick_log_path,
        quarantine_path=quarantine_path,
        quote_sessions=quote_sessions,
        endpoint_source=endpoint_source,
        path_quotes_complete=path_quotes_complete,
    )
    summary["records_appended"] = append_jsonl(labeled, output_path)
    write_summary(summary, summary_path)
    return summary


def main() -> int:
    summary = materialize_a2()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
