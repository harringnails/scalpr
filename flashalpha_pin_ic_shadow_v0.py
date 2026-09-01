"""Isolated 0DTE iron-condor paper simulation for the SPY pin study.

This module is exploratory and non-inferential. It can only read provider data
and append shadow-study artifacts; it has no order, admission, or execution path.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import math
import random
import subprocess
import time
import uuid
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable

import requests

import flashalpha_pin_scanner_v0 as scanner
import flashalpha_shadow_v0 as shadow


PREREG_PATH = Path(__file__).with_name("flashalpha_pin_ic_prereg_v0.json")
PREREG_SHA256 = "5a66278075eb1eeacafbafcb59c7971b7ea882c6b79edf5746a36f76550c19a8"
SCHEMA_VERSION = "flashalpha-pin-ic-entry-v0"
SETTLEMENT_SCHEMA_VERSION = "flashalpha-pin-ic-settlement-v0"
REPORT_SCHEMA_VERSION = "flashalpha-pin-ic-report-v0"
QUOTE_SCHEMA_VERSION = "flashalpha-pin-ic-provider-record-v0"
NULL_VERSION = "flashalpha-pin-ic-session-block-sign-null-v0"
MODULE_VERSION = "flashalpha-pin-ic-shadow-v0"
QUOTE_LEDGER = Path("flashalpha_pin_ic_quotes_v0.jsonl")
STUDY_LEDGER = Path("flashalpha_pin_ic_study_v0.jsonl")
REPORT_PATH = Path("flashalpha_pin_ic_report_v0.json")
ALPACA_KEY_SERVICE = "scalpr.alpaca.paper.key"
ALPACA_SECRET_SERVICE = "scalpr.alpaca.paper.secret"
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/SPY/bars"
FLASHALPHA_OPTIONQUOTE_URL = "https://lab.flashalpha.com/optionquote/SPY"
ENTRY_START_ET = wall_time(14, 0)
ENTRY_END_ET = wall_time(14, 5, 59)
WING_WIDTH = 2.0
CONTRACT_MULTIPLIER = 100
COMMISSION_PER_LEG = 0.65
SLIPPAGE_PER_LEG = 0.05
TOTAL_COMMISSION = 4 * COMMISSION_PER_LEG
TOTAL_SLIPPAGE_POINTS = 4 * SLIPPAGE_PER_LEG
MAX_QUOTE_AGE_SECONDS = 60.0
DEFAULT_TARGET_DAYS = 60
DEFAULT_PERMUTATIONS = 5000
DEFAULT_BLOCK_SESSIONS = 5


class ProviderError(RuntimeError):
    pass


def _preregistration() -> dict[str, Any]:
    body = PREREG_PATH.read_bytes()
    actual = hashlib.sha256(body).hexdigest()
    if actual != PREREG_SHA256:
        raise RuntimeError("frozen iron-condor pre-registration hash mismatch")
    return json.loads(body)


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
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except (ValueError, AttributeError):
        return None


def _deep(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _authority_fields() -> dict[str, Any]:
    return {
        "study_status": "EXPLORATORY - NON-INFERENTIAL",
        "advisory_only": True,
        "observational_only": True,
        "is_inferential": False,
        "is_qualifying": False,
        "admission_authority": False,
        "execution_authority": False,
    }


def _finalize(record: dict[str, Any]) -> dict[str, Any]:
    complete = dict(record)
    complete.update(_authority_fields())
    complete["record_hash"] = shadow.canonical_hash(complete)
    return complete


def _read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    return scanner.read_jsonl(path)


def _append_unique(path: Path | str, record: dict[str, Any]) -> bool:
    for row in _read_jsonl(path):
        if row.get("record_type") == record.get("record_type") \
                and row.get("session_date") == record.get("session_date"):
            return False
    shadow.append_jsonl(path, record)
    return True


def _keychain_secret(service: str, *, account: str | None = None) -> str:
    command = ["security", "find-generic-password"]
    if account:
        command.extend(["-a", account])
    command.extend(["-s", service, "-w"])
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise ProviderError("macOS Keychain command unavailable") from exc
    value = (result.stdout or "").strip()
    if result.returncode != 0 or not value:
        raise ProviderError(f"credential unavailable for Keychain service {service}")
    return value


def load_alpaca_credentials(*, account: str | None = None) -> tuple[str, str]:
    return (
        _keychain_secret(ALPACA_KEY_SERVICE, account=account),
        _keychain_secret(ALPACA_SECRET_SERVICE, account=account),
    )


class FlashAlphaOptionChainClient:
    """Read one live same-day option chain without retries."""

    def __init__(
        self,
        api_key: str,
        *,
        session: Any = None,
        clock: Callable[[], datetime] = shadow.utc_now,
        timeout_seconds: float = 12.0,
    ):
        self.session = session or requests.Session()
        self.clock = clock
        self.timeout_seconds = timeout_seconds
        self.session.headers.update({
            "X-Api-Key": str(api_key).strip(),
            "Accept": "application/json",
            "User-Agent": MODULE_VERSION,
        })

    def fetch(self, *, expiry: str, candidate_hash: str) -> dict[str, Any]:
        request_at = self.clock().astimezone(timezone.utc)
        run_id = str(uuid.uuid4())
        try:
            response = self.session.get(
                FLASHALPHA_OPTIONQUOTE_URL,
                params={"expiry": expiry},
                timeout=self.timeout_seconds,
            )
            response_at = self.clock().astimezone(timezone.utc)
            payload = _safe_json(response)
            status = shadow._status_for_response(response.status_code, payload)
            error = None
        except requests.RequestException as exc:
            response_at = self.clock().astimezone(timezone.utc)
            response = None
            payload = None
            status = "REQUEST_ERROR"
            error = type(exc).__name__
        headers = getattr(response, "headers", {}) if response is not None else {}
        record = {
            "schema_version": QUOTE_SCHEMA_VERSION,
            "record_type": "IC_OPTION_CHAIN_SNAPSHOT",
            "run_id": run_id,
            "vendor": "FlashAlpha",
            "endpoint": "/optionquote/SPY",
            "account_tier": "GROWTH",
            "symbol": "SPY",
            "expiry": expiry,
            "candidate_hash": candidate_hash,
            "request_at_utc": request_at.isoformat(),
            "response_at_utc": response_at.isoformat(),
            "status": status,
            "http_status": int(response.status_code) if response is not None else None,
            "returned_values": payload if status == "AVAILABLE" else None,
            "error_payload": payload if status != "AVAILABLE" else None,
            "error": error,
            "rate_limit": {
                "limit": headers.get("X-RateLimit-Limit"),
                "remaining": headers.get("X-RateLimit-Remaining"),
                "reset": headers.get("X-RateLimit-Reset"),
                "retry_after_seconds": headers.get("Retry-After"),
            },
            "preregistration_sha256": PREREG_SHA256,
        }
        return _finalize(record)


class AlpacaCloseClient:
    """Read the completed 15:59 ET SIP minute bar; never submit an order."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        session: Any = None,
        clock: Callable[[], datetime] = shadow.utc_now,
        timeout_seconds: float = 12.0,
    ):
        self.session = session or requests.Session()
        self.clock = clock
        self.timeout_seconds = timeout_seconds
        self.session.headers.update({
            "APCA-API-KEY-ID": str(api_key).strip(),
            "APCA-API-SECRET-KEY": str(api_secret).strip(),
            "Accept": "application/json",
            "User-Agent": MODULE_VERSION,
        })

    def fetch(self, *, session_date: str) -> dict[str, Any]:
        target_date = date.fromisoformat(session_date)
        close_minute_et = datetime.combine(target_date, wall_time(15, 59), tzinfo=scanner.NEW_YORK)
        start = close_minute_et.astimezone(timezone.utc)
        end = (close_minute_et + timedelta(minutes=2)).astimezone(timezone.utc)
        request_at = self.clock().astimezone(timezone.utc)
        try:
            response = self.session.get(
                ALPACA_BARS_URL,
                params={
                    "timeframe": "1Min", "start": start.isoformat(), "end": end.isoformat(),
                    "limit": 10, "adjustment": "raw", "feed": "sip", "sort": "asc",
                },
                timeout=self.timeout_seconds,
            )
            response_at = self.clock().astimezone(timezone.utc)
            payload = _safe_json(response)
            status = "AVAILABLE" if response.status_code == 200 and isinstance(payload, dict) else "UNAVAILABLE"
            error = None if status == "AVAILABLE" else f"HTTP_{response.status_code}"
        except requests.RequestException as exc:
            response_at = self.clock().astimezone(timezone.utc)
            response = None
            payload = None
            status = "REQUEST_ERROR"
            error = type(exc).__name__
        bars = payload.get("bars", []) if isinstance(payload, dict) else []
        selected = None
        for bar in bars if isinstance(bars, list) else []:
            stamp = _parse_datetime(bar.get("t")) if isinstance(bar, dict) else None
            close = _positive(bar.get("c")) if isinstance(bar, dict) else None
            if stamp and close and stamp.astimezone(scanner.NEW_YORK).date() == target_date \
                    and stamp.astimezone(scanner.NEW_YORK).time() == wall_time(15, 59):
                selected = {"timestamp_utc": stamp.isoformat(), "close": close}
                break
        if status == "AVAILABLE" and selected is None:
            status = "UNAVAILABLE"
            error = "MISSING_15_59_ET_SIP_BAR"
        record = {
            "schema_version": QUOTE_SCHEMA_VERSION,
            "record_type": "IC_UNDERLYING_CLOSE_SNAPSHOT",
            "vendor": "Alpaca",
            "endpoint": "/v2/stocks/SPY/bars",
            "feed": "SIP",
            "symbol": "SPY",
            "session_date": session_date,
            "request_at_utc": request_at.isoformat(),
            "response_at_utc": response_at.isoformat(),
            "status": status,
            "http_status": int(response.status_code) if response is not None else None,
            "selected_bar": selected,
            "error": error,
            "preregistration_sha256": PREREG_SHA256,
        }
        return _finalize(record)


def _entry_candidate(rows: list[dict[str, Any]], session_date: str) -> dict[str, Any] | None:
    eligible = []
    for row in rows:
        if row.get("record_type") != "PIN_CANDIDATE" or row.get("session_date") != session_date:
            continue
        observed = _parse_datetime(row.get("observed_at_utc"))
        if observed is None:
            continue
        local = observed.astimezone(scanner.NEW_YORK)
        if ENTRY_START_ET <= local.time() <= ENTRY_END_ET:
            eligible.append((observed, row))
    return min(eligible, key=lambda item: item[0])[1] if eligible else None


def _chain_rows(chain_record: dict[str, Any]) -> list[dict[str, Any]]:
    payload = chain_record.get("returned_values")
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("contracts", "options", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        if payload.get("strike") is not None:
            return [payload]
    return []


def _right(row: dict[str, Any]) -> str | None:
    value = str(row.get("type") or row.get("option_type") or "").strip().upper()
    if value in {"C", "CALL"}:
        return "C"
    if value in {"P", "PUT"}:
        return "P"
    return None


def _nearest_strike(strikes: set[float], target: float, *, right: str) -> float | None:
    if not strikes:
        return None
    tie_direction = (lambda strike: strike) if right == "C" else (lambda strike: -strike)
    return min(strikes, key=lambda strike: (abs(strike - target), tie_direction(strike)))


def _contract(rows: list[dict[str, Any]], *, strike: float, right: str) -> dict[str, Any] | None:
    matches = [
        row for row in rows
        if _right(row) == right and _finite(row.get("strike")) == strike
    ]
    return matches[0] if len(matches) == 1 else None


def _quote_evidence(row: dict[str, Any] | None, *, response_at: datetime) -> tuple[dict[str, Any] | None, str | None]:
    if row is None:
        return None, "CONTRACT_MISSING_OR_DUPLICATE"
    bid = _positive(row.get("bid"))
    ask = _positive(row.get("ask"))
    if bid is None or ask is None or ask < bid:
        return None, "QUOTE_ZERO_OR_CROSSED"
    observed = _parse_datetime(row.get("lastUpdate") or row.get("last_update") or row.get("updated_at"))
    if observed is None:
        return None, "QUOTE_TIMESTAMP_MISSING"
    age = (response_at - observed).total_seconds()
    if age < 0:
        return None, "QUOTE_TIMESTAMP_AFTER_RESPONSE"
    if age > MAX_QUOTE_AGE_SECONDS:
        return None, "QUOTE_STALE"
    return {
        "strike": float(row["strike"]),
        "right": _right(row),
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2.0,
        "quote_observed_at_utc": observed.isoformat(),
        "quote_age_seconds": age,
    }, None


def build_entry(candidate: dict[str, Any], chain_record: dict[str, Any]) -> dict[str, Any]:
    _preregistration()
    session_date = str(candidate.get("session_date") or "")
    unavailable_reason = None
    if chain_record.get("status") != "AVAILABLE":
        unavailable_reason = f"OPTION_CHAIN_{chain_record.get('status', 'UNAVAILABLE')}"
    rows = _chain_rows(chain_record)
    candidate_at = _parse_datetime(candidate.get("observed_at_utc"))
    response_at = _parse_datetime(chain_record.get("response_at_utc"))
    if unavailable_reason is None and (candidate_at is None or response_at is None):
        unavailable_reason = "ENTRY_TIMESTAMPS_MISSING"
    if unavailable_reason is None:
        response_local = response_at.astimezone(scanner.NEW_YORK)
        delay = (response_at - candidate_at).total_seconds()
        if response_local.date().isoformat() != session_date \
                or not ENTRY_START_ET <= response_local.time() <= ENTRY_END_ET:
            unavailable_reason = "OPTION_CHAIN_OUTSIDE_FIXED_ENTRY_WINDOW"
        elif not 0 <= delay <= MAX_QUOTE_AGE_SECONDS:
            unavailable_reason = "OPTION_CHAIN_NOT_CONTEMPORANEOUS_WITH_CANDIDATE"
    call_wall = _positive(_deep(candidate, "evidence", "pocket", "call_wall"))
    put_wall = _positive(_deep(candidate, "evidence", "pocket", "put_wall"))
    if unavailable_reason is None and (call_wall is None or put_wall is None):
        unavailable_reason = "CANDIDATE_WALLS_MISSING"
    expiry_rows = [row for row in rows if str(row.get("expiry")) == session_date]
    call_strikes = {_finite(row.get("strike")) for row in expiry_rows if _right(row) == "C"}
    put_strikes = {_finite(row.get("strike")) for row in expiry_rows if _right(row) == "P"}
    call_strikes.discard(None)
    put_strikes.discard(None)
    short_call = _nearest_strike(call_strikes, call_wall, right="C") if call_wall else None
    short_put = _nearest_strike(put_strikes, put_wall, right="P") if put_wall else None
    long_call = short_call + WING_WIDTH if short_call is not None else None
    long_put = short_put - WING_WIDTH if short_put is not None else None
    if unavailable_reason is None and (
        short_call is None or short_put is None or long_call not in call_strikes or long_put not in put_strikes
    ):
        unavailable_reason = "EXACT_TWO_POINT_WINGS_UNAVAILABLE"
    if unavailable_reason is None and short_put >= short_call:
        unavailable_reason = "SHORT_STRIKES_INVALID"

    response_at = response_at or datetime.min.replace(tzinfo=timezone.utc)
    legs: dict[str, Any] = {}
    if unavailable_reason is None:
        selections = {
            "long_put": (long_put, "P"), "short_put": (short_put, "P"),
            "short_call": (short_call, "C"), "long_call": (long_call, "C"),
        }
        for name, (strike, right) in selections.items():
            quote, reason = _quote_evidence(
                _contract(expiry_rows, strike=float(strike), right=right), response_at=response_at,
            )
            if reason:
                unavailable_reason = f"{name.upper()}_{reason}"
                break
            legs[name] = quote

    gross_credit = None
    adjusted_credit = None
    if unavailable_reason is None:
        gross_credit = (
            legs["short_put"]["mid"] + legs["short_call"]["mid"]
            - legs["long_put"]["mid"] - legs["long_call"]["mid"]
        )
        adjusted_credit = gross_credit - TOTAL_SLIPPAGE_POINTS
        if adjusted_credit <= 0:
            unavailable_reason = "NONPOSITIVE_CREDIT_AFTER_SLIPPAGE"

    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "IC_ENTRY",
        "session_date": session_date,
        "symbol": "SPY",
        "status": "AVAILABLE" if unavailable_reason is None else "UNAVAILABLE",
        "unavailable_reason": unavailable_reason,
        "candidate_hash": candidate.get("record_hash"),
        "candidate_grade": candidate.get("grade"),
        "cohort": "HIGH" if candidate.get("grade") == "HIGH_PIN_PRESSURE" else "NON_HIGH_CONTROL",
        "candidate_observed_at_utc": candidate.get("observed_at_utc"),
        "option_chain_record_hash": chain_record.get("record_hash"),
        "option_chain_response_at_utc": chain_record.get("response_at_utc"),
        "call_wall": call_wall,
        "put_wall": put_wall,
        "strikes": {
            "long_put": long_put, "short_put": short_put,
            "short_call": short_call, "long_call": long_call,
        },
        "legs": legs,
        "gross_mid_credit_points": gross_credit,
        "entry_slippage_points": TOTAL_SLIPPAGE_POINTS,
        "adjusted_credit_points": adjusted_credit,
        "commission_dollars": TOTAL_COMMISSION,
        "quantity": 1,
        "contract_multiplier": CONTRACT_MULTIPLIER,
        "preregistration_sha256": PREREG_SHA256,
    }
    return _finalize(record)


def capture_entry(
    *,
    session_date: str,
    pin_study_path: Path | str = scanner.STUDY_LEDGER,
    quote_path: Path | str = QUOTE_LEDGER,
    ic_study_path: Path | str = STUDY_LEDGER,
    key_loader: Callable[[], str] = shadow.load_keychain_api_key,
    client_factory: Callable[[str], FlashAlphaOptionChainClient] = FlashAlphaOptionChainClient,
) -> dict[str, Any]:
    date.fromisoformat(session_date)
    existing = [
        row for row in _read_jsonl(ic_study_path)
        if row.get("record_type") == "IC_ENTRY" and row.get("session_date") == session_date
    ]
    if existing:
        return {"status": "ALREADY_RECORDED", "rate_limited": False, "entry": existing[0]}
    candidate = _entry_candidate(_read_jsonl(pin_study_path), session_date)
    if candidate is None:
        rate_limited = False
        record = _finalize({
            "schema_version": SCHEMA_VERSION, "record_type": "IC_ENTRY",
            "session_date": session_date, "symbol": "SPY", "status": "UNAVAILABLE",
            "unavailable_reason": "NO_FIXED_WINDOW_CANDIDATE",
            "preregistration_sha256": PREREG_SHA256,
        })
    else:
        try:
            client = client_factory(key_loader())
            chain_record = client.fetch(expiry=session_date, candidate_hash=candidate["record_hash"])
        except (shadow.KeychainError, ProviderError) as exc:
            chain_record = _finalize({
                "schema_version": QUOTE_SCHEMA_VERSION,
                "record_type": "IC_OPTION_CHAIN_SNAPSHOT",
                "session_date": session_date,
                "status": "UNAVAILABLE",
                "error": type(exc).__name__,
                "preregistration_sha256": PREREG_SHA256,
            })
        shadow.append_jsonl(quote_path, chain_record)
        rate_limited = chain_record.get("status") == "RATE_LIMITED"
        record = build_entry(candidate, chain_record)
    appended = _append_unique(ic_study_path, record)
    return {
        "status": record["status"], "rate_limited": rate_limited,
        "record_appended": appended, "entry": record,
    }


def settlement_pnl(entry: dict[str, Any], close_price: float) -> dict[str, float]:
    if entry.get("status") != "AVAILABLE":
        raise ValueError("entry is unavailable")
    close = _positive(close_price)
    strikes = entry.get("strikes") or {}
    long_put = _positive(strikes.get("long_put"))
    short_put = _positive(strikes.get("short_put"))
    short_call = _positive(strikes.get("short_call"))
    long_call = _positive(strikes.get("long_call"))
    credit = _positive(entry.get("adjusted_credit_points"))
    if None in (close, long_put, short_put, short_call, long_call, credit):
        raise ValueError("settlement inputs incomplete")
    put_loss = max(short_put - close, 0.0) - max(long_put - close, 0.0)
    call_loss = max(close - short_call, 0.0) - max(close - long_call, 0.0)
    gross = (credit - put_loss - call_loss) * CONTRACT_MULTIPLIER
    net = gross - TOTAL_COMMISSION
    return {
        "put_intrinsic_loss_points": put_loss,
        "call_intrinsic_loss_points": call_loss,
        "gross_settlement_pnl_dollars": gross,
        "commission_dollars": TOTAL_COMMISSION,
        "net_pnl_dollars": net,
    }


def settle_session(
    *,
    session_date: str,
    quote_path: Path | str = QUOTE_LEDGER,
    ic_study_path: Path | str = STUDY_LEDGER,
    credential_loader: Callable[[], tuple[str, str]] = load_alpaca_credentials,
    client_factory: Callable[[str, str], AlpacaCloseClient] = AlpacaCloseClient,
) -> dict[str, Any]:
    date.fromisoformat(session_date)
    entries = [
        row for row in _read_jsonl(ic_study_path)
        if row.get("record_type") == "IC_ENTRY" and row.get("session_date") == session_date
    ]
    existing = [
        row for row in _read_jsonl(ic_study_path)
        if row.get("record_type") == "IC_SETTLEMENT" and row.get("session_date") == session_date
    ]
    if existing:
        return {"status": "ALREADY_RECORDED", "settlement": existing[0]}
    entry = entries[0] if entries else None
    close_record = None
    unavailable_reason = None
    if entry is None:
        unavailable_reason = "ENTRY_RECORD_MISSING"
    elif entry.get("status") != "AVAILABLE":
        unavailable_reason = "ENTRY_UNAVAILABLE"
    else:
        try:
            api_key, api_secret = credential_loader()
            close_record = client_factory(api_key, api_secret).fetch(session_date=session_date)
        except ProviderError as exc:
            close_record = _finalize({
                "schema_version": QUOTE_SCHEMA_VERSION,
                "record_type": "IC_UNDERLYING_CLOSE_SNAPSHOT",
                "session_date": session_date,
                "status": "UNAVAILABLE",
                "error": type(exc).__name__,
                "preregistration_sha256": PREREG_SHA256,
            })
        shadow.append_jsonl(quote_path, close_record)
        if close_record.get("status") != "AVAILABLE":
            unavailable_reason = "ALPACA_SIP_CLOSE_UNAVAILABLE"
    close_price = _positive(_deep(close_record, "selected_bar", "close")) if close_record else None
    pnl = settlement_pnl(entry, close_price) if unavailable_reason is None and close_price else None
    record = {
        "schema_version": SETTLEMENT_SCHEMA_VERSION,
        "record_type": "IC_SETTLEMENT",
        "session_date": session_date,
        "symbol": "SPY",
        "status": "AVAILABLE" if pnl is not None else "UNAVAILABLE",
        "unavailable_reason": unavailable_reason,
        "entry_record_hash": entry.get("record_hash") if entry else None,
        "candidate_grade": entry.get("candidate_grade") if entry else None,
        "cohort": entry.get("cohort") if entry else None,
        "entry_credit_points": entry.get("gross_mid_credit_points") if entry else None,
        "adjusted_credit_points": entry.get("adjusted_credit_points") if entry else None,
        "strikes": entry.get("strikes") if entry else None,
        "close_record_hash": close_record.get("record_hash") if close_record else None,
        "spy_close": close_price,
        "pnl": pnl,
        "win": pnl["net_pnl_dollars"] > 0 if pnl else None,
        "preregistration_sha256": PREREG_SHA256,
    }
    record = _finalize(record)
    appended = _append_unique(ic_study_path, record)
    return {"status": record["status"], "record_appended": appended, "settlement": record}


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(_deep(row, "pnl", "net_pnl_dollars")) for row in rows]
    ordered_rows = sorted(rows, key=lambda row: float(_deep(row, "pnl", "net_pnl_dollars")))
    bottom_count = max(1, math.ceil(len(values) * 0.10)) if values else 0
    return {
        "available_days": len(values),
        "mean_net_pnl_dollars": mean(values) if values else None,
        "median_net_pnl_dollars": median(values) if values else None,
        "total_net_pnl_dollars": sum(values) if values else None,
        "net_expectancy_after_costs_dollars": mean(values) if values else None,
        "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
        "loss_rate": sum(value < 0 for value in values) / len(values) if values else None,
        "minimum_net_pnl_dollars": min(values) if values else None,
        "p05_net_pnl_dollars": _quantile(values, 0.05),
        "p10_net_pnl_dollars": _quantile(values, 0.10),
        "bottom_decile_mean_net_pnl_dollars": (
            mean(sorted(values)[:bottom_count]) if values else None
        ),
        "worst_sessions": [
            {"session_date": row["session_date"], "net_pnl_dollars": _deep(row, "pnl", "net_pnl_dollars")}
            for row in ordered_rows[:5]
        ],
    }


def session_block_pnl_null(
    increments: list[dict[str, Any]],
    *,
    permutations: int = DEFAULT_PERMUTATIONS,
    block_sessions: int = DEFAULT_BLOCK_SESSIONS,
    seed: int = 20260901,
) -> dict[str, Any]:
    clean = sorted(
        (row for row in increments if _finite(row.get("incremental_pnl_dollars")) is not None),
        key=lambda row: row["session_date"],
    )
    if not clean:
        return {"available": False, "reason": "NO_HIGH_DAY_PNL_INCREMENTS", "null_version": NULL_VERSION}
    sessions = [row["session_date"] for row in clean]
    blocks = [set(sessions[i:i + block_sessions]) for i in range(0, len(sessions), block_sessions)]
    observed = mean(float(row["incremental_pnl_dollars"]) for row in clean)
    rng = random.Random(seed)
    null_values = []
    for _ in range(max(1, int(permutations))):
        signs = {}
        for block in blocks:
            sign = 1 if rng.random() >= 0.5 else -1
            for session in block:
                signs[session] = sign
        null_values.append(mean(
            float(row["incremental_pnl_dollars"]) * signs[row["session_date"]] for row in clean
        ))
    p_value = (1 + sum(value >= observed for value in null_values)) / (len(null_values) + 1)
    return {
        "available": True,
        "null_version": NULL_VERSION,
        "metric": "HIGH_DAY_NET_PNL_MINUS_NON_HIGH_MEAN_NET_PNL",
        "n_high_days": len(clean),
        "n_session_blocks": len(blocks),
        "block_sessions": block_sessions,
        "n_permutations": max(1, int(permutations)),
        "seed": seed,
        "observed_mean_lift_dollars": observed,
        "one_sided_p_value": p_value,
        "finite_sample_plus_one": True,
        "is_inferential": False,
    }


def build_report(
    *,
    ic_study_path: Path | str = STUDY_LEDGER,
    target_days: int = DEFAULT_TARGET_DAYS,
    permutations: int = DEFAULT_PERMUTATIONS,
    clock: Callable[[], datetime] = shadow.utc_now,
) -> dict[str, Any]:
    _preregistration()
    if target_days <= 0:
        raise ValueError("target_days must be positive")
    rows = _read_jsonl(ic_study_path)
    entries = {row["session_date"]: row for row in rows if row.get("record_type") == "IC_ENTRY"}
    settlements = [
        row for row in rows
        if row.get("record_type") == "IC_SETTLEMENT" and row.get("status") == "AVAILABLE"
    ]
    settlements.sort(key=lambda row: row["session_date"])
    high = [row for row in settlements if row.get("cohort") == "HIGH"]
    control = [row for row in settlements if row.get("cohort") == "NON_HIGH_CONTROL"]
    high_stats = _group_stats(high)
    control_stats = _group_stats(control)
    control_mean = control_stats["mean_net_pnl_dollars"]
    increments = [
        {
            "session_date": row["session_date"],
            "incremental_pnl_dollars": float(_deep(row, "pnl", "net_pnl_dollars")) - control_mean,
        }
        for row in high
    ] if control_mean is not None else []
    per_session = []
    settlement_by_date = {row["session_date"]: row for row in settlements}
    all_settlement_rows = {
        row["session_date"]: row for row in rows if row.get("record_type") == "IC_SETTLEMENT"
    }
    for session_date, entry in sorted(entries.items()):
        settlement = all_settlement_rows.get(session_date)
        per_session.append({
            "session_date": session_date,
            "cohort": entry.get("cohort"),
            "candidate_grade": entry.get("candidate_grade"),
            "status": settlement.get("status") if settlement else entry.get("status"),
            "unavailable_reason": (
                settlement.get("unavailable_reason") if settlement else entry.get("unavailable_reason")
            ),
            "entry_credit_points": entry.get("gross_mid_credit_points"),
            "adjusted_credit_points": entry.get("adjusted_credit_points"),
            "spy_close": settlement.get("spy_close") if settlement else None,
            "net_pnl_dollars": _deep(settlement, "pnl", "net_pnl_dollars") if settlement else None,
        })
    unavailable = [row for row in per_session if row["status"] != "AVAILABLE"]
    unavailable_reasons = [str(row["unavailable_reason"] or "UNKNOWN") for row in unavailable]
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at_utc": clock().astimezone(timezone.utc).isoformat(),
        "preregistration_sha256": PREREG_SHA256,
        "validation_target_available_days": target_days,
        "available_days": len(settlements),
        "study_phase": (
            "EXPLORATORY_REVIEW_READY - NON-INFERENTIAL"
            if len(settlements) >= target_days else "ACCUMULATING - NON-INFERENTIAL"
        ),
        "all_priceable_days": _group_stats(settlements),
        "high_candidate_days": high_stats,
        "non_high_control_days": control_stats,
        "high_minus_non_high_mean_net_pnl_dollars": (
            high_stats["mean_net_pnl_dollars"] - control_mean
            if high_stats["mean_net_pnl_dollars"] is not None and control_mean is not None else None
        ),
        "session_block_matched_null": session_block_pnl_null(
            increments, permutations=permutations,
        ),
        "unavailable_days": len(unavailable),
        "unavailable_reasons": {
            reason: unavailable_reasons.count(reason)
            for reason in sorted(set(unavailable_reasons))
        },
        "per_session": per_session,
        "primary_metric": "HIGH mean net P&L minus non-HIGH mean net P&L",
        "interpretation": "Shadow paper result only; no edge, admission, or execution verdict.",
    }
    report.update(_authority_fields())
    report["report_hash"] = shadow.canonical_hash(report)
    return report


def study_poll(
    *,
    polls: int,
    interval_seconds: int = scanner.POLL_INTERVAL_SECONDS,
    raw_path: Path | str = scanner.RAW_LEDGER,
    pin_study_path: Path | str = scanner.STUDY_LEDGER,
    quote_path: Path | str = QUOTE_LEDGER,
    ic_study_path: Path | str = STUDY_LEDGER,
    scan_kwargs: dict[str, Any] | None = None,
    option_key_loader: Callable[[], str] = shadow.load_keychain_api_key,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    clock: Callable[[], datetime] = shadow.utc_now,
) -> dict[str, Any]:
    if polls <= 0 or interval_seconds <= 0:
        raise ValueError("polls and interval_seconds must be positive")
    started = monotonic()
    summaries = []
    captures = []
    for index in range(polls):
        kwargs = dict(scan_kwargs or {})
        kwargs.update({"raw_path": raw_path, "study_path": pin_study_path, "clock": clock})
        summary = scanner.scan_once(**kwargs)
        summaries.append(summary)
        now = clock().astimezone(scanner.NEW_YORK)
        if ENTRY_START_ET <= now.time() <= ENTRY_END_ET:
            capture_result = capture_entry(
                session_date=now.date().isoformat(), pin_study_path=pin_study_path,
                quote_path=quote_path, ic_study_path=ic_study_path,
                key_loader=option_key_loader,
            )
            captures.append(capture_result)
            if capture_result.get("rate_limited"):
                return {
                    "polls_completed": len(summaries),
                    "stopped_reason": "OPTION_CHAIN_RATE_LIMITED",
                    "entry_capture_attempts": len(captures),
                    "execution_authority": False,
                }
        if summary.get("rate_limited"):
            return {
                "polls_completed": len(summaries), "stopped_reason": "RATE_LIMITED",
                "entry_capture_attempts": len(captures), "execution_authority": False,
            }
        if index + 1 < polls:
            target = started + (index + 1) * interval_seconds
            sleep(max(0.0, target - monotonic()))
    return {
        "polls_completed": len(summaries), "stopped_reason": "COMPLETE",
        "entry_capture_attempts": len(captures), "execution_authority": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Exploratory SPY pin iron-condor shadow study")
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture-entry")
    capture.add_argument("--session-date", required=True)
    capture.add_argument("--pin-study", type=Path, default=scanner.STUDY_LEDGER)
    capture.add_argument("--quote-output", type=Path, default=QUOTE_LEDGER)
    capture.add_argument("--ic-study", type=Path, default=STUDY_LEDGER)
    settle = subparsers.add_parser("settle")
    settle.add_argument("--session-date", required=True)
    settle.add_argument("--quote-output", type=Path, default=QUOTE_LEDGER)
    settle.add_argument("--ic-study", type=Path, default=STUDY_LEDGER)
    report = subparsers.add_parser("report")
    report.add_argument("--ic-study", type=Path, default=STUDY_LEDGER)
    report.add_argument("--report-output", type=Path, default=REPORT_PATH)
    report.add_argument("--target-days", type=int, default=DEFAULT_TARGET_DAYS)
    report.add_argument("--permutations", type=int, default=DEFAULT_PERMUTATIONS)
    poll_parser = subparsers.add_parser("study-poll")
    poll_parser.add_argument("--polls", type=int, required=True)
    poll_parser.add_argument("--interval-seconds", type=int, default=scanner.POLL_INTERVAL_SECONDS)
    poll_parser.add_argument("--raw-output", type=Path, default=scanner.RAW_LEDGER)
    poll_parser.add_argument("--pin-study", type=Path, default=scanner.STUDY_LEDGER)
    poll_parser.add_argument("--quote-output", type=Path, default=QUOTE_LEDGER)
    poll_parser.add_argument("--ic-study", type=Path, default=STUDY_LEDGER)
    poll_parser.add_argument("--keychain-account", default=getpass.getuser())
    args = parser.parse_args()

    account = getattr(args, "keychain_account", getpass.getuser())
    if args.command == "capture-entry":
        result = capture_entry(
            session_date=args.session_date, pin_study_path=args.pin_study,
            quote_path=args.quote_output, ic_study_path=args.ic_study,
            key_loader=lambda: shadow.load_keychain_api_key(account=account),
        )
    elif args.command == "settle":
        result = settle_session(
            session_date=args.session_date, quote_path=args.quote_output,
            ic_study_path=args.ic_study,
            credential_loader=lambda: load_alpaca_credentials(account=account),
        )
    elif args.command == "report":
        result = build_report(
            ic_study_path=args.ic_study, target_days=args.target_days,
            permutations=args.permutations,
        )
        scanner.write_json_atomic(args.report_output, result)
    else:
        result = study_poll(
            polls=args.polls, interval_seconds=args.interval_seconds,
            raw_path=args.raw_output, pin_study_path=args.pin_study,
            quote_path=args.quote_output, ic_study_path=args.ic_study,
            scan_kwargs={
                "configured_tier": "GROWTH", "call_budget": 3,
                "key_loader": lambda: shadow.load_keychain_api_key(account=account),
            },
            option_key_loader=lambda: shadow.load_keychain_api_key(account=account),
        )
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
