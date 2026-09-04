#!/usr/bin/env python3
"""Shared read-only inputs and point-in-time outcome labeling for signal studies."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
HORIZONS_MINUTES = (5, 15, 30, 60)
MAX_QUOTE_OFFSET_SECONDS = 5.0
LOCK_DATE = date(2026, 9, 4)


def parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
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


def session_bounds(session_date: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(session_date)
    open_et = datetime.combine(day, time(9, 30), tzinfo=ET)
    close_et = datetime.combine(day, time(16, 0), tzinfo=ET)
    return open_et.astimezone(timezone.utc), close_et.astimezone(timezone.utc)


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_quote(row: dict[str, Any]) -> dict[str, Any] | None:
    provider_ts = parse_timestamp(row.get("provider_ts"))
    try:
        bid = float(row.get("bid") or 0)
        ask = float(row.get("ask") or 0)
    except (TypeError, ValueError):
        return None
    if provider_ts is None or bid <= 0 or ask <= 0 or bid > ask:
        return None
    received_at = parse_timestamp(row.get("utc_time"))
    return {
        "ask": ask,
        "bid": bid,
        "mid": (bid + ask) / 2.0,
        "provider_ts": provider_ts,
        "received_at": received_at,
        "receipt_age_seconds": (
            round((received_at - provider_ts).total_seconds(), 6) if received_at else None
        ),
        "source": "tick_log.csv:provider_ts:two_sided_mid",
    }


def load_session_quotes(path: Path, session_date: str, *, include_premarket: bool) -> list[dict[str, Any]]:
    open_at, close_at = session_bounds(session_date)
    lower = open_at - timedelta(hours=5, minutes=30) if include_premarket else open_at
    quotes: list[dict[str, Any]] = []
    if not path.exists():
        return quotes
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("symbol") or "").upper() != "SPY":
                continue
            quote = clean_quote(row)
            if quote and lower <= quote["provider_ts"] < close_at:
                quotes.append(quote)
    return sorted(quotes, key=lambda quote: quote["provider_ts"])


def _wall_price(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("price")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def flash_effective_timestamp(row: dict[str, Any]) -> datetime | None:
    timestamps = [parse_timestamp(row.get("observed_at_utc"))]
    freshness = (row.get("evidence") or {}).get("data_freshness") or {}
    timestamps.extend(parse_timestamp(value) for value in (freshness.get("as_of_by_endpoint") or {}).values())
    usable = [timestamp for timestamp in timestamps if timestamp is not None]
    return max(usable) if usable else None


def flash_is_fresh(row: dict[str, Any]) -> bool:
    freshness = (row.get("evidence") or {}).get("data_freshness") or {}
    return freshness.get("status") == "FRESH" and all(
        state == "AVAILABLE" for state in (freshness.get("endpoint_states") or {}).values()
    )


def load_flash_candidates(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("record_type") != "PIN_CANDIDATE" or row.get("symbol") != "SPY":
                continue
            effective_ts = flash_effective_timestamp(row)
            if effective_ts is None:
                continue
            normalized = dict(row)
            normalized["effective_ts"] = effective_ts
            rows.append(normalized)
    return sorted(rows, key=lambda row: row["effective_ts"])


def flash_values(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence") or {}
    return {
        "call_wall": _wall_price(evidence.get("call_wall")),
        "gamma_flip": _wall_price(evidence.get("gamma_flip")),
        "gamma_regime": str(evidence.get("gamma_regime") or "").lower() or None,
        "put_wall": _wall_price(evidence.get("put_wall")),
        "spot": _wall_price(evidence.get("spot")),
    }


def latest_at_or_before(quotes: list[dict[str, Any]], target: datetime) -> dict[str, Any] | None:
    selected = None
    for quote in quotes:
        if quote["provider_ts"] > target:
            break
        selected = quote
    if selected is None or (target - selected["provider_ts"]).total_seconds() > MAX_QUOTE_OFFSET_SECONDS:
        return None
    return selected


def earliest_at_or_after(quotes: list[dict[str, Any]], target: datetime) -> dict[str, Any] | None:
    for quote in quotes:
        if quote["provider_ts"] < target:
            continue
        return quote if (quote["provider_ts"] - target).total_seconds() <= MAX_QUOTE_OFFSET_SECONDS else None
    return None


def quote_provenance(quote: dict[str, Any] | None, target: datetime) -> dict[str, Any]:
    if quote is None:
        return {"provider_ts": None, "target_offset_seconds": None, "status": "MISSING"}
    return {
        "provider_ts": quote["provider_ts"].isoformat(),
        "receipt_age_seconds": quote.get("receipt_age_seconds"),
        "source": quote["source"],
        "status": "AVAILABLE",
        "target_offset_seconds": round((quote["provider_ts"] - target).total_seconds(), 6),
    }


def flash_provenance(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {"status": "MISSING"}
    evidence = row.get("evidence") or {}
    return {
        "candidate_id": row.get("candidate_id"),
        "effective_ts": row["effective_ts"].isoformat(),
        "freshness": evidence.get("data_freshness"),
        "observed_at_utc": row.get("observed_at_utc"),
        "record_hash": row.get("record_hash"),
        "source": "FlashAlpha:pin_candidate_shadow_ledger",
        "status": "FRESH" if flash_is_fresh(row) else "STALE_OR_UNAVAILABLE",
    }


def label_outcome(quotes: list[dict[str, Any]], t0: datetime) -> dict[str, Any]:
    anchor = latest_at_or_before(quotes, t0)
    endpoints = {
        horizon: earliest_at_or_after(quotes, t0 + timedelta(minutes=horizon))
        for horizon in HORIZONS_MINUTES
    }
    missing = (["anchor"] if anchor is None else []) + [
        f"{horizon}m" for horizon, quote in endpoints.items() if quote is None
    ]
    returns = {
        f"return_{horizon}m": (
            round(math.log(quote["mid"] / anchor["mid"]), 8)
            if anchor is not None and quote is not None else None
        )
        for horizon, quote in endpoints.items()
    }
    return {
        "a2_outcome_status": "A2-AVAILABLE" if not missing else "A2-UNAVAILABLE",
        "anchor_mid": round(anchor["mid"], 6) if anchor else None,
        "missing_points": missing,
        "provenance": {
            "anchor": quote_provenance(anchor, t0),
            "endpoints": {
                f"{horizon}m": quote_provenance(quote, t0 + timedelta(minutes=horizon))
                for horizon, quote in endpoints.items()
            },
        },
        "returns": returns,
    }


def base_record(*, study_id: str, session_date: str, prereg_path: Path) -> dict[str, Any]:
    return {
        "evidence_classification": "PROSPECTIVE_POST_FREEZE",
        "frozen_prereg_path": prereg_path.name,
        "frozen_prereg_sha256": file_sha256(prereg_path),
        "is_inferential": False,
        "record_type": "SIGNAL_EPISODE_EVALUATION",
        "schema_version": f"{study_id}-episode-v0",
        "session_date": session_date,
        "study_id": study_id,
        "provenance": {},
        "verdict_status": "UNDERPOWERED",
    }


def finalize_record(record: dict[str, Any]) -> dict[str, Any]:
    finalized = dict(record)
    finalized["record_hash"] = canonical_hash(finalized)
    return finalized


def append_once(path: Path, record: dict[str, Any]) -> bool:
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if existing.get("study_id") == record.get("study_id") and existing.get("session_date") == record.get("session_date"):
                    return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return True


def prelock(session_date: str) -> bool:
    return date.fromisoformat(session_date) <= LOCK_DATE


def gap_exceeds(quotes: Iterable[dict[str, Any]], seconds: float = 5.0) -> bool:
    prior = None
    for quote in quotes:
        if prior is not None and (quote["provider_ts"] - prior).total_seconds() > seconds:
            return True
        prior = quote["provider_ts"]
    return False
