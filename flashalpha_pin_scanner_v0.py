"""Exploratory, non-inferential SPY pin-candidate shadow study.

The module reads only FlashAlpha shadow endpoints. It has no execution,
admission, collector, server, A2, dense-store, or prospective-cohort authority.
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import random
import time
import uuid
from datetime import date, datetime, time as wall_time, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import flashalpha_shadow_v0 as shadow


SCHEMA_VERSION = "flashalpha-pin-candidate-v0"
OUTCOME_SCHEMA_VERSION = "flashalpha-pin-outcome-v0"
REPORT_SCHEMA_VERSION = "flashalpha-pin-report-v0"
NULL_VERSION = "flashalpha-pin-session-block-sign-null-v0"
RAW_LEDGER = Path("flashalpha_shadow_v0.jsonl")
STUDY_LEDGER = Path("flashalpha_pin_study_v0.jsonl")
REPORT_PATH = Path("flashalpha_pin_report_v0.json")
SCANNER_ENDPOINTS = ("gex", "levels", "zero_dte")
DEFAULT_CALL_BUDGET = 3
POLL_INTERVAL_SECONDS = 300
MAX_FRESHNESS_SECONDS = 300.0
FINAL_TWO_HOURS = 2.0
LOW_SCORE_CEILING = 40.0
HIGH_SCORE_FLOOR = 70.0
DEFAULT_TARGET_DAYS = 20
DEFAULT_PERMUTATIONS = 5000
DEFAULT_BLOCK_SESSIONS = 5
NEW_YORK = ZoneInfo("America/New_York")

GRADE_RANK = {
    "UNKNOWN": 0,
    "ANTI_PIN_NEGATIVE_GAMMA": 1,
    "LOW_PIN_PRESSURE": 2,
    "MODERATE_PIN_PRESSURE": 3,
    "HIGH_PIN_PRESSURE": 4,
}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _deep(mapping: Any, *path: str) -> Any:
    current = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_number(*values: Any, positive: bool = False) -> float | None:
    converter = _positive if positive else _finite
    for value in values:
        number = converter(value)
        if number is not None:
            return number
    return None


def _payload(records: Iterable[dict[str, Any]], endpoint: str) -> dict[str, Any] | None:
    for row in records:
        if row.get("endpoint_name") == endpoint and row.get("status") == "AVAILABLE":
            value = row.get("returned_values")
            if isinstance(value, dict):
                return value
    return None


def _record_by_endpoint(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("endpoint_name")): row
        for row in records if row.get("endpoint_name")
    }


def _strike_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in ("strikes", "strike_data", "by_strike"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def build_strike_map(
    gex_payload: dict[str, Any] | None,
    zero_dte_payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Merge provider strike rows without synthesizing missing OI or GEX."""
    combined: dict[float, dict[str, Any]] = {}
    for prefix, payload in (("full_chain", gex_payload), ("zero_dte", zero_dte_payload)):
        for source in _strike_rows(payload):
            strike = _positive(source.get("strike"))
            if strike is None:
                continue
            row = combined.setdefault(strike, {"strike": strike})
            for field in ("call_oi", "put_oi", "call_gex", "put_gex", "net_gex"):
                value = _finite(source.get(field))
                row[f"{prefix}_{field}"] = value
                if prefix == "zero_dte" or field not in row:
                    if value is not None:
                        row[field] = value
            row[f"{prefix}_source_present"] = True
    return [combined[strike] for strike in sorted(combined)]


def _provider_wall_candidates(
    levels_payload: dict[str, Any] | None,
    zero_dte_payload: dict[str, Any] | None,
    field: str,
) -> list[dict[str, Any]]:
    candidates = []
    for source_name, payload in (
        ("zero_dte.levels", zero_dte_payload),
        ("levels", levels_payload),
    ):
        value = _positive(_deep(payload, "levels", field))
        if value is not None:
            candidates.append({"price": value, "source": source_name, "field": field})
    return candidates


def _select_wall(
    *,
    side: str,
    spot: float | None,
    provider_candidates: list[dict[str, Any]],
    strike_map: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if spot is None:
        return None
    if side == "call":
        eligible = [row for row in provider_candidates if row["price"] > spot]
        if eligible:
            return min(eligible, key=lambda row: (row["price"] - spot, row["source"]))
        derived = [
            row for row in strike_map
            if row["strike"] > spot and _finite(row.get("call_gex")) is not None
        ]
        if derived:
            peak = max(derived, key=lambda row: (abs(row["call_gex"]), -row["strike"]))
            return {
                "price": peak["strike"], "source": "gex.strikes",
                "field": "peak_absolute_call_gex_above_spot",
            }
    else:
        eligible = [row for row in provider_candidates if row["price"] < spot]
        if eligible:
            return min(eligible, key=lambda row: (spot - row["price"], row["source"]))
        derived = [
            row for row in strike_map
            if row["strike"] < spot and _finite(row.get("put_gex")) is not None
        ]
        if derived:
            peak = max(derived, key=lambda row: (abs(row["put_gex"]), row["strike"]))
            return {
                "price": peak["strike"], "source": "gex.strikes",
                "field": "peak_absolute_put_gex_below_spot",
            }
    return None


def _gamma_regime(gex: dict[str, Any] | None, zero_dte: dict[str, Any] | None) -> str:
    raw = _deep(zero_dte, "regime", "label")
    if raw is None and isinstance(gex, dict):
        raw = gex.get("net_gex_label") or gex.get("regime")
    clean = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if clean in {"positive", "positive_gamma", "long_gamma", "long"}:
        return "positive"
    if clean in {"negative", "negative_gamma", "short_gamma", "short"}:
        return "negative"
    net_gex = _finite(gex.get("net_gex")) if isinstance(gex, dict) else None
    if net_gex is not None:
        if net_gex > 0:
            return "positive"
        if net_gex < 0:
            return "negative"
    return "unknown"


def _freshness(
    raw_records: list[dict[str, Any]],
    payloads: dict[str, dict[str, Any] | None],
    observed_at: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    endpoint_records = _record_by_endpoint(raw_records)
    states = {
        name: (endpoint_records.get(name) or {}).get("status", "MISSING")
        for name in SCANNER_ENDPOINTS
    }
    ages: dict[str, float | None] = {}
    as_of: dict[str, str | None] = {}
    reasons = []
    for name in SCANNER_ENDPOINTS:
        payload = payloads.get(name)
        parsed = _parse_datetime(payload.get("as_of")) if isinstance(payload, dict) else None
        as_of[name] = parsed.isoformat() if parsed else None
        if parsed is None:
            ages[name] = None
            reasons.append(f"{name.upper()}_AS_OF_MISSING")
            continue
        age = (observed_at - parsed).total_seconds()
        ages[name] = age
        if age < -5:
            reasons.append(f"{name.upper()}_PROVIDER_TIME_AFTER_RECEIPT")
        elif age > max_age_seconds:
            reasons.append(f"{name.upper()}_STALE")
    unavailable = [name for name, state in states.items() if state != "AVAILABLE"]
    if unavailable:
        reasons.extend(f"{name.upper()}_{states[name]}" for name in unavailable)
    status = "FRESH" if not reasons else "UNAVAILABLE_OR_STALE"
    return {
        "status": status,
        "max_age_seconds": float(max_age_seconds),
        "endpoint_states": states,
        "age_seconds_by_endpoint": ages,
        "as_of_by_endpoint": as_of,
        "reasons": sorted(set(reasons)),
    }


def _median_strike_spacing(strike_map: list[dict[str, Any]]) -> float | None:
    strikes = sorted({row["strike"] for row in strike_map})
    differences = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return median(differences) if differences else None


def build_candidate(
    raw_records: list[dict[str, Any]],
    *,
    observed_at: datetime | None = None,
    max_age_seconds: float = MAX_FRESHNESS_SECONDS,
) -> dict[str, Any]:
    observed = observed_at or shadow.utc_now()
    if observed.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    observed = observed.astimezone(timezone.utc)
    gex = _payload(raw_records, "gex")
    levels = _payload(raw_records, "levels")
    zero_dte = _payload(raw_records, "zero_dte")
    payloads = {"gex": gex, "levels": levels, "zero_dte": zero_dte}
    strike_map = build_strike_map(gex, zero_dte)

    spot = _first_number(
        zero_dte.get("underlying_price") if zero_dte else None,
        gex.get("underlying_price") if gex else None,
        levels.get("underlying_price") if levels else None,
        positive=True,
    )
    call_wall = _select_wall(
        side="call", spot=spot,
        provider_candidates=_provider_wall_candidates(levels, zero_dte, "call_wall"),
        strike_map=strike_map,
    )
    put_wall = _select_wall(
        side="put", spot=spot,
        provider_candidates=_provider_wall_candidates(levels, zero_dte, "put_wall"),
        strike_map=strike_map,
    )
    call_price = call_wall["price"] if call_wall else None
    put_price = put_wall["price"] if put_wall else None
    pocket_width = (
        call_price - put_price
        if call_price is not None and put_price is not None and call_price > put_price
        else None
    )
    pocket_position = (
        (spot - put_price) / pocket_width
        if spot is not None and pocket_width is not None else None
    )
    gamma_flip = _first_number(
        _deep(zero_dte, "regime", "gamma_flip"),
        gex.get("gamma_flip") if gex else None,
        _deep(levels, "levels", "gamma_flip"),
        positive=True,
    )
    magnet = _first_number(
        _deep(zero_dte, "pin_risk", "magnet_strike"),
        _deep(zero_dte, "pin_risk", "pin_target"),
        _deep(zero_dte, "levels", "zero_dte_magnet"),
        _deep(levels, "levels", "zero_dte_magnet"),
        positive=True,
    )
    max_pain = _first_number(
        _deep(zero_dte, "pin_risk", "max_pain"),
        _deep(zero_dte, "levels", "max_pain"),
        _deep(levels, "levels", "max_pain"),
        positive=True,
    )
    pin_score = _first_number(
        _deep(zero_dte, "pin_risk", "pin_score"),
        _deep(zero_dte, "pin_risk", "score"),
    )
    time_to_close = _first_number(zero_dte.get("time_to_close_hours") if zero_dte else None)
    gex_share = _first_number(
        _deep(zero_dte, "exposures", "pct_of_total_gex"),
        _deep(zero_dte, "exposures", "zero_dte_gex_share_pct"),
    )
    regime = _gamma_regime(gex, zero_dte)
    freshness = _freshness(raw_records, payloads, observed, max_age_seconds)
    strike_spacing = _median_strike_spacing(strike_map)

    missing = []
    required = {
        "spot": spot,
        "call_wall": call_price,
        "put_wall": put_price,
        "gamma_flip": gamma_flip,
        "pin_score": pin_score,
        "time_to_close_hours": time_to_close,
        "magnet_strike": magnet,
        "strike_map": strike_map or None,
    }
    for name, value in required.items():
        if value is None:
            missing.append(name)

    grade_reasons = []
    if time_to_close is not None and time_to_close <= 0:
        grade = "UNKNOWN"
        grade_reasons.append("POST_CLOSE_OR_INVALID_TIME")
    elif regime == "negative":
        grade = "ANTI_PIN_NEGATIVE_GAMMA"
        grade_reasons.append("NEGATIVE_GAMMA_BREAKOUT_GATE")
    elif freshness["status"] != "FRESH":
        grade = "UNKNOWN"
        grade_reasons.append("DATA_NOT_FRESH_AND_COMPLETE")
    elif regime != "positive":
        grade = "UNKNOWN"
        grade_reasons.append("GAMMA_REGIME_NOT_POSITIVE")
    elif missing:
        grade = "UNKNOWN"
        grade_reasons.append("REQUIRED_EVIDENCE_MISSING")
    elif time_to_close is None:
        grade = "UNKNOWN"
        grade_reasons.append("TIME_TO_CLOSE_MISSING")
    elif pin_score is None or pin_score < 0 or pin_score > 100:
        grade = "UNKNOWN"
        grade_reasons.append("PIN_SCORE_OUT_OF_RANGE")
    elif pin_score < LOW_SCORE_CEILING:
        grade = "LOW_PIN_PRESSURE"
        grade_reasons.append("PROVIDER_PIN_SCORE_BELOW_40")
    elif pin_score < HIGH_SCORE_FLOOR:
        grade = "MODERATE_PIN_PRESSURE"
        grade_reasons.append("PROVIDER_PIN_SCORE_40_TO_69")
    elif time_to_close <= FINAL_TWO_HOURS:
        grade = "HIGH_PIN_PRESSURE"
        grade_reasons.append("PROVIDER_PIN_SCORE_AT_LEAST_70_FINAL_TWO_HOURS")
    else:
        grade = "MODERATE_PIN_PRESSURE"
        grade_reasons.append("EARLY_SESSION_CAP_AT_MODERATE")

    session_date = observed.astimezone(NEW_YORK).date().isoformat()
    evidence = {
        "spot": spot,
        "gamma_regime": regime,
        "gamma_flip": gamma_flip,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "spot_to_call_wall_bps": (
            (call_price - spot) / spot * 10_000
            if spot is not None and call_price is not None else None
        ),
        "spot_to_put_wall_bps": (
            (spot - put_price) / spot * 10_000
            if spot is not None and put_price is not None else None
        ),
        "pocket": {
            "put_wall": put_price,
            "call_wall": call_price,
            "width": pocket_width,
            "spot_normalized_position": pocket_position,
            "spot_inside": (
                put_price <= spot <= call_price
                if spot is not None and put_price is not None and call_price is not None
                else None
            ),
        },
        "strike_map": strike_map,
        "median_strike_spacing": strike_spacing,
        "near_magnet_tolerance": strike_spacing / 2 if strike_spacing else None,
        "zero_dte_gex_share_pct": gex_share,
        "magnet_strike": magnet,
        "max_pain": max_pain,
        "max_pain_magnet_agree": (
            math.isclose(max_pain, magnet, rel_tol=0, abs_tol=1e-9)
            if max_pain is not None and magnet is not None else None
        ),
        "pin_score": pin_score,
        "time_to_close_hours": time_to_close,
        "data_freshness": freshness,
        "missing_required_evidence": sorted(missing),
    }
    raw_hashes = [row.get("record_hash") for row in raw_records if row.get("record_hash")]
    candidate_id = shadow.canonical_hash({
        "schema_version": SCHEMA_VERSION,
        "symbol": "SPY",
        "observed_at_utc": observed.isoformat(),
        "grade": grade,
        "evidence": evidence,
        "raw_record_hashes": raw_hashes,
    })
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "PIN_CANDIDATE",
        "candidate_id": candidate_id,
        "run_id": raw_records[0].get("run_id") if raw_records else None,
        "symbol": "SPY",
        "session_date": session_date,
        "observed_at_utc": observed.isoformat(),
        "grade": grade,
        "grade_reasons": grade_reasons,
        "evidence": evidence,
        "raw_record_hashes": raw_hashes,
        "study_status": "EXPLORATORY - NON-INFERENTIAL",
        "advisory_only": True,
        "observational_only": True,
        "is_inferential": False,
        "is_qualifying": False,
        "admission_authority": False,
        "execution_authority": False,
    }
    record["record_hash"] = shadow.canonical_hash(record)
    return record


def _key_error_record(exc: Exception, run_id: str, observed: datetime) -> dict[str, Any]:
    return shadow.finalize_record({
        "schema_version": shadow.SCHEMA_VERSION,
        "module_version": shadow.MODULE_VERSION,
        "run_id": run_id,
        "vendor": "FlashAlpha",
        "endpoint_name": None,
        "endpoint_path": None,
        "account_tier": "UNKNOWN",
        "symbol": "SPY",
        "request_at_utc": observed.isoformat(),
        "response_at_utc": observed.isoformat(),
        "status": "KEYCHAIN_UNAVAILABLE",
        "http_status": None,
        "returned_values": None,
        "error": str(exc),
        "rate_limit": shadow._empty_rate_limit(),
        "advisory_only": True,
        "observational_only": True,
        "is_qualifying": False,
        "admission_authority": False,
        "execution_authority": False,
    })


def _local_skip_record(
    endpoint_name: str,
    *,
    run_id: str,
    observed: datetime,
    configured_tier: str,
    status: str,
    reason: str,
) -> dict[str, Any]:
    endpoint = shadow.ENDPOINTS[endpoint_name]
    return shadow.finalize_record({
        "schema_version": shadow.SCHEMA_VERSION,
        "module_version": shadow.MODULE_VERSION,
        "run_id": run_id,
        "vendor": "FlashAlpha",
        "base_url": shadow.BASE_URL,
        "endpoint_name": endpoint_name,
        "endpoint_path": endpoint.path,
        "endpoint_semantics": endpoint.semantics,
        "documented_minimum_tier": endpoint.documented_minimum_tier,
        "account_tier": configured_tier,
        "symbol": "SPY",
        "request_at_utc": observed.isoformat(),
        "response_at_utc": observed.isoformat(),
        "status": status,
        "http_status": None,
        "returned_values": None,
        "error": reason,
        "rate_limit": shadow._empty_rate_limit(),
        "advisory_only": True,
        "observational_only": True,
        "is_qualifying": False,
        "admission_authority": False,
        "execution_authority": False,
    })


def _tier_endpoints(configured_tier: str) -> set[str]:
    tier = configured_tier.upper()
    if tier == "FREE":
        return set()
    if tier == "BASIC":
        return {"gex", "levels"}
    return set(SCANNER_ENDPOINTS)


def scan_once(
    *,
    raw_path: Path | str = RAW_LEDGER,
    study_path: Path | str = STUDY_LEDGER,
    configured_tier: str = "GROWTH",
    call_budget: int = DEFAULT_CALL_BUDGET,
    max_age_seconds: float = MAX_FRESHNESS_SECONDS,
    key_loader: Callable[[], str] = shadow.load_keychain_api_key,
    client_factory: Callable[..., shadow.FlashAlphaShadowClient] = shadow.FlashAlphaShadowClient,
    clock: Callable[[], datetime] = shadow.utc_now,
) -> dict[str, Any]:
    if call_budget < 0:
        raise ValueError("call_budget cannot be negative")
    observed = clock().astimezone(timezone.utc)
    run_id = str(uuid.uuid4())
    raw_records = []
    tier = configured_tier.upper()
    enabled = _tier_endpoints(tier)
    live_names = [name for name in SCANNER_ENDPOINTS if name in enabled]
    client = None
    if live_names and call_budget > 0:
        try:
            api_key = key_loader()
        except shadow.KeychainError as exc:
            raw_records.append(_key_error_record(exc, run_id, observed))
        else:
            client = client_factory(api_key)
    attempted = 0
    if not raw_records:
        for endpoint_name in SCANNER_ENDPOINTS:
            if endpoint_name not in enabled:
                raw_records.append(_local_skip_record(
                    endpoint_name, run_id=run_id, observed=observed,
                    configured_tier=tier, status="TIER_SKIPPED",
                    reason=f"{endpoint_name} not requested for declared {tier} tier",
                ))
                continue
            if attempted >= call_budget or client is None:
                raw_records.append(_local_skip_record(
                    endpoint_name, run_id=run_id, observed=observed,
                    configured_tier=tier, status="CALL_BUDGET_SKIPPED",
                    reason="per-poll HTTP call budget reached",
                ))
                continue
            record = client.fetch(
                endpoint=shadow.ENDPOINTS[endpoint_name],
                symbol="SPY",
                run_id=run_id,
                configured_tier=tier,
            )
            attempted += 1
            raw_records.append(record)
            if record["status"] == "RATE_LIMITED":
                break
    for record in raw_records:
        shadow.append_jsonl(raw_path, record)
    candidate = build_candidate(
        raw_records, observed_at=observed, max_age_seconds=max_age_seconds,
    )
    shadow.append_jsonl(study_path, candidate)
    return {
        "run_id": run_id,
        "raw_records_written": len(raw_records),
        "http_requests_attempted": attempted,
        "call_budget": call_budget,
        "candidate_record_hash": candidate["record_hash"],
        "grade": candidate["grade"],
        "rate_limited": any(row.get("status") == "RATE_LIMITED" for row in raw_records),
        "study_status": "EXPLORATORY - NON-INFERENTIAL",
        "execution_authority": False,
    }


def poll(
    *,
    polls: int,
    interval_seconds: int = POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    **scan_kwargs: Any,
) -> dict[str, Any]:
    if polls <= 0 or interval_seconds <= 0:
        raise ValueError("polls and interval_seconds must be positive")
    summaries = []
    for index in range(polls):
        summary = scan_once(**scan_kwargs)
        summaries.append(summary)
        if summary["rate_limited"]:
            return {"polls_completed": len(summaries), "stopped_reason": "RATE_LIMITED"}
        if index + 1 < polls:
            sleep(interval_seconds)
    return {"polls_completed": len(summaries), "stopped_reason": "COMPLETE"}


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        return []
    rows = []
    with source.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _candidate_time(row: dict[str, Any]) -> datetime:
    return _parse_datetime(row.get("observed_at_utc")) or datetime.min.replace(tzinfo=timezone.utc)


def _select_session_candidate(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    intraday = [
        row for row in rows
        if row.get("record_type") == "PIN_CANDIDATE"
        and _finite(_deep(row, "evidence", "time_to_close_hours")) is not None
        and _finite(_deep(row, "evidence", "time_to_close_hours")) > 0
    ]
    if not intraday:
        return None
    return sorted(
        intraday,
        key=lambda row: (-GRADE_RANK.get(row.get("grade"), 0), _candidate_time(row)),
    )[0]


def _select_close_observation(rows: list[dict[str, Any]], session_date: str) -> dict[str, Any] | None:
    target = date.fromisoformat(session_date)
    candidates = []
    for row in rows:
        if row.get("record_type") != "PIN_CANDIDATE" or row.get("session_date") != session_date:
            continue
        freshness = _deep(row, "evidence", "data_freshness")
        if not isinstance(freshness, dict) or freshness.get("status") != "FRESH":
            continue
        provider_as_of = _parse_datetime(_deep(freshness, "as_of_by_endpoint", "zero_dte"))
        spot = _positive(_deep(row, "evidence", "spot"))
        if provider_as_of is None or spot is None:
            continue
        local = provider_as_of.astimezone(NEW_YORK)
        if local.date() == target and local.time() >= wall_time(16, 0):
            candidates.append((provider_as_of, row))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def append_unique_session_record(path: Path | str, record: dict[str, Any]) -> bool:
    for row in read_jsonl(path):
        if row.get("record_type") == record.get("record_type") \
                and row.get("session_date") == record.get("session_date"):
            return False
    shadow.append_jsonl(path, record)
    return True


def score_session_outcome(
    session_date: str,
    *,
    study_path: Path | str = STUDY_LEDGER,
) -> dict[str, Any]:
    date.fromisoformat(session_date)
    rows = [row for row in read_jsonl(study_path) if row.get("session_date") == session_date]
    selected = _select_session_candidate(rows)
    close_row = _select_close_observation(rows, session_date)
    unavailable_reason = None
    if selected is None:
        unavailable_reason = "NO_INTRADAY_CANDIDATE"
    elif close_row is None:
        unavailable_reason = "NO_FRESH_AT_OR_AFTER_CLOSE_OBSERVATION"

    close_price = _positive(_deep(close_row, "evidence", "spot")) if close_row else None
    put_wall = _positive(_deep(selected, "evidence", "pocket", "put_wall")) if selected else None
    call_wall = _positive(_deep(selected, "evidence", "pocket", "call_wall")) if selected else None
    magnet = _positive(_deep(selected, "evidence", "magnet_strike")) if selected else None
    tolerance = _positive(_deep(selected, "evidence", "near_magnet_tolerance")) if selected else None
    available = (
        unavailable_reason is None and close_price is not None
        and put_wall is not None and call_wall is not None
        and magnet is not None and tolerance is not None
    )
    if unavailable_reason is None and not available:
        unavailable_reason = "SELECTED_CANDIDATE_EVIDENCE_INCOMPLETE"

    record = {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "record_type": "PIN_SESSION_OUTCOME",
        "session_date": session_date,
        "symbol": "SPY",
        "status": "AVAILABLE" if available else "UNAVAILABLE",
        "unavailable_reason": unavailable_reason,
        "selected_candidate_hash": selected.get("record_hash") if selected else None,
        "selected_candidate_grade": selected.get("grade") if selected else None,
        "close_observation_hash": close_row.get("record_hash") if close_row else None,
        "close_price": close_price,
        "pocket": {"put_wall": put_wall, "call_wall": call_wall},
        "magnet_strike": magnet,
        "near_magnet_tolerance": tolerance,
        "close_inside_pocket": (
            put_wall <= close_price <= call_wall if available else None
        ),
        "close_near_magnet": (
            abs(close_price - magnet) <= tolerance if available else None
        ),
        "close_to_magnet_distance": (
            abs(close_price - magnet) if available else None
        ),
        "study_status": "EXPLORATORY - NON-INFERENTIAL",
        "is_inferential": False,
        "is_qualifying": False,
        "admission_authority": False,
        "execution_authority": False,
    }
    record["record_hash"] = shadow.canonical_hash(record)
    record["record_appended"] = append_unique_session_record(study_path, record)
    return record


def session_block_sign_null(
    increments: list[dict[str, Any]],
    *,
    permutations: int = DEFAULT_PERMUTATIONS,
    block_sessions: int = DEFAULT_BLOCK_SESSIONS,
    seed: int = 20260831,
) -> dict[str, Any]:
    clean = sorted(
        (row for row in increments if _finite(row.get("incremental_hit")) is not None),
        key=lambda row: row["session_date"],
    )
    if not clean:
        return {"available": False, "reason": "no_high_day_increments", "null_version": NULL_VERSION}
    sessions = [row["session_date"] for row in clean]
    block_sessions = max(1, int(block_sessions))
    blocks = [set(sessions[i:i + block_sessions]) for i in range(0, len(sessions), block_sessions)]
    observed = mean(float(row["incremental_hit"]) for row in clean)
    rng = random.Random(seed)
    null = []
    for _ in range(max(1, int(permutations))):
        signs = {}
        for block in blocks:
            sign = 1 if rng.random() >= 0.5 else -1
            for session in block:
                signs[session] = sign
        null.append(mean(float(row["incremental_hit"]) * signs[row["session_date"]]
                         for row in clean))
    p_value = (1 + sum(value >= observed for value in null)) / (len(null) + 1)
    return {
        "available": True,
        "null_version": NULL_VERSION,
        "metric": "mean_high_day_magnet_hit_minus_non_high_base_rate",
        "n_high_days": len(clean),
        "n_session_blocks": len(blocks),
        "block_sessions": block_sessions,
        "n_permutations": max(1, int(permutations)),
        "seed": seed,
        "observed_mean_lift": observed,
        "one_sided_p_value": p_value,
        "finite_sample_plus_one": True,
        "is_inferential": False,
    }


def _rate(values: list[bool]) -> float | None:
    return sum(bool(value) for value in values) / len(values) if values else None


def build_report(
    *,
    study_path: Path | str = STUDY_LEDGER,
    target_days: int = DEFAULT_TARGET_DAYS,
    permutations: int = DEFAULT_PERMUTATIONS,
    clock: Callable[[], datetime] = shadow.utc_now,
) -> dict[str, Any]:
    if target_days <= 0:
        raise ValueError("target_days must be positive")
    outcomes = [
        row for row in read_jsonl(study_path)
        if row.get("record_type") == "PIN_SESSION_OUTCOME" and row.get("status") == "AVAILABLE"
    ]
    outcomes.sort(key=lambda row: row["session_date"])
    high = [row for row in outcomes if row.get("selected_candidate_grade") == "HIGH_PIN_PRESSURE"]
    non_high = [row for row in outcomes if row.get("selected_candidate_grade") != "HIGH_PIN_PRESSURE"]
    high_hits = [bool(row["close_near_magnet"]) for row in high]
    non_high_hits = [bool(row["close_near_magnet"]) for row in non_high]
    all_hits = [bool(row["close_near_magnet"]) for row in outcomes]
    high_rate = _rate(high_hits)
    non_high_rate = _rate(non_high_hits)
    all_rate = _rate(all_hits)
    increments = [
        {"session_date": row["session_date"], "incremental_hit": float(row["close_near_magnet"]) - non_high_rate}
        for row in high
    ] if non_high_rate is not None else []
    null = session_block_sign_null(increments, permutations=permutations)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at_utc": clock().astimezone(timezone.utc).isoformat(),
        "study_status": (
            "EXPLORATORY_REVIEW_READY - NON-INFERENTIAL"
            if len(outcomes) >= target_days else "ACCUMULATING - NON-INFERENTIAL"
        ),
        "validation_target_days": target_days,
        "available_session_days": len(outcomes),
        "high_candidate_days": len(high),
        "non_high_days": len(non_high),
        "high_candidate_close_near_magnet_rate": high_rate,
        "all_day_close_near_magnet_base_rate": all_rate,
        "non_high_close_near_magnet_base_rate": non_high_rate,
        "lift_over_all_day_base_rate": (
            high_rate - all_rate if high_rate is not None and all_rate is not None else None
        ),
        "lift_over_non_high_base_rate": (
            high_rate - non_high_rate
            if high_rate is not None and non_high_rate is not None else None
        ),
        "high_candidate_close_inside_pocket_rate": _rate([
            bool(row["close_inside_pocket"]) for row in high
        ]),
        "all_day_close_inside_pocket_base_rate": _rate([
            bool(row["close_inside_pocket"]) for row in outcomes
        ]),
        "session_block_matched_null": null,
        "primary_metric": "lift_over_non_high_base_rate",
        "interpretation": "Directional shadow-study read only; no edge or execution verdict.",
        "is_inferential": False,
        "is_qualifying": False,
        "admission_authority": False,
        "execution_authority": False,
    }
    report["report_hash"] = shadow.canonical_hash(report)
    return report


def write_json_atomic(path: Path | str, value: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def _common_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tier", default="GROWTH")
    parser.add_argument("--raw-output", type=Path, default=RAW_LEDGER)
    parser.add_argument("--study-output", type=Path, default=STUDY_LEDGER)
    parser.add_argument("--max-age-seconds", type=float, default=MAX_FRESHNESS_SECONDS)
    parser.add_argument("--budget", type=int, default=DEFAULT_CALL_BUDGET)
    parser.add_argument("--keychain-account", default=getpass.getuser())


def main() -> int:
    parser = argparse.ArgumentParser(description="Exploratory FlashAlpha SPY pin scanner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan", help="perform one three-endpoint shadow scan")
    _common_scan_arguments(scan_parser)
    poll_parser = subparsers.add_parser("poll", help="poll at a fixed interval")
    _common_scan_arguments(poll_parser)
    poll_parser.add_argument("--polls", type=int, required=True)
    poll_parser.add_argument("--interval-seconds", type=int, default=POLL_INTERVAL_SECONDS)
    outcome_parser = subparsers.add_parser("score-outcome", help="score one session after close")
    outcome_parser.add_argument("--session-date", required=True)
    outcome_parser.add_argument("--study-output", type=Path, default=STUDY_LEDGER)
    report_parser = subparsers.add_parser("report", help="report lift over base rates")
    report_parser.add_argument("--study-output", type=Path, default=STUDY_LEDGER)
    report_parser.add_argument("--report-output", type=Path, default=REPORT_PATH)
    report_parser.add_argument("--target-days", type=int, default=DEFAULT_TARGET_DAYS)
    report_parser.add_argument("--permutations", type=int, default=DEFAULT_PERMUTATIONS)
    args = parser.parse_args()

    if args.command in {"scan", "poll"}:
        kwargs = {
            "raw_path": args.raw_output,
            "study_path": args.study_output,
            "configured_tier": args.tier,
            "call_budget": args.budget,
            "max_age_seconds": args.max_age_seconds,
            "key_loader": lambda: shadow.load_keychain_api_key(account=args.keychain_account),
        }
        result = scan_once(**kwargs) if args.command == "scan" else poll(
            polls=args.polls, interval_seconds=args.interval_seconds, **kwargs,
        )
    elif args.command == "score-outcome":
        result = score_session_outcome(args.session_date, study_path=args.study_output)
    else:
        result = build_report(
            study_path=args.study_output,
            target_days=args.target_days,
            permutations=args.permutations,
        )
        write_json_atomic(args.report_output, result)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
