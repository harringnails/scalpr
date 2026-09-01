"""Isolated, read-only FlashAlpha shadow-signal ingestion.

This module has no execution, admission, gate, collector, or server authority.
It only appends point-in-time provider observations to its own JSONL ledger.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

import requests


SCHEMA_VERSION = "flashalpha-shadow-observation-v0"
MODULE_VERSION = "flashalpha-shadow-ingestion-v0"
BASE_URL = "https://lab.flashalpha.com/v1"
KEYCHAIN_SERVICE = "scalpr.flashalpha.api"
DEFAULT_OUTPUT_PATH = Path("flashalpha_shadow_v0.jsonl")
DEFAULT_CALL_BUDGET = 1


@dataclass(frozen=True)
class EndpointSpec:
    name: str
    path: str
    documented_minimum_tier: str
    semantics: str


ENDPOINTS = {
    "gex": EndpointSpec(
        "gex", "/exposure/gex/{symbol}", "FREE_EQUITIES_BASIC_ETF_INDEX",
        "gamma exposure by strike",
    ),
    "levels": EndpointSpec(
        "levels", "/exposure/levels/{symbol}", "FREE_EQUITIES_BASIC_ETF_INDEX",
        "gamma flip, walls, highest-OI strike, and 0DTE magnet",
    ),
    "maxpain": EndpointSpec(
        "maxpain", "/maxpain/{symbol}", "BASIC",
        "max-pain analysis and provider pin probability",
    ),
    "zero_dte": EndpointSpec(
        "zero_dte", "/exposure/zero-dte/{symbol}", "GROWTH",
        "canonical 0DTE analytics including the pin_risk object",
    ),
    "flow_pin_risk": EndpointSpec(
        "flow_pin_risk", "/flow/pin-risk/{symbol}", "GROWTH",
        "live flow-derived pin-risk score",
    ),
}


class KeychainError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def canonical_hash(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def load_keychain_api_key(
    *,
    service: str = KEYCHAIN_SERVICE,
    account: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Read the API key without placing it in source, arguments, or logs."""
    command = ["security", "find-generic-password"]
    if account:
        command.extend(["-a", account])
    command.extend(["-s", service, "-w"])
    try:
        result = runner(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise KeychainError("macOS Keychain command is unavailable") from exc
    key = (result.stdout or "").strip()
    if result.returncode != 0 or not key:
        raise KeychainError(f"API key not found in Keychain service {service}")
    return key


def _safe_json(response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _header(headers: Any, name: str) -> str | None:
    if not hasattr(headers, "get"):
        return None
    value = headers.get(name)
    return str(value) if value is not None else None


def _provider_error(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for field in ("message", "detail", "error", "title"):
        value = payload.get(field)
        if value is not None:
            return str(value)[:500]
    return None


class FlashAlphaShadowClient:
    """One-shot HTTP client with no retries and no write authority."""

    def __init__(
        self,
        api_key: str,
        *,
        session=None,
        timeout_seconds: float = 8.0,
        clock: Callable[[], datetime] = utc_now,
        timer: Callable[[], float] = time.monotonic,
    ):
        clean_key = str(api_key).strip()
        if not clean_key:
            raise ValueError("FlashAlpha API key is required")
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock
        self.timer = timer
        self.session.headers.update({
            "X-Api-Key": clean_key,
            "Accept": "application/json",
            "User-Agent": MODULE_VERSION,
        })

    def fetch(
        self,
        *,
        endpoint: EndpointSpec,
        symbol: str,
        run_id: str,
        configured_tier: str,
    ) -> dict[str, Any]:
        ticker = normalize_symbol(symbol)
        path = endpoint.path.format(symbol=quote(ticker, safe=".-"))
        request_at = self.clock()
        started = self.timer()
        try:
            response = self.session.get(
                f"{BASE_URL}{path}", timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            response_at = self.clock()
            record = self._base_record(
                endpoint, ticker, run_id, configured_tier, request_at, response_at,
            )
            record.update({
                "status": "REQUEST_ERROR",
                "http_status": None,
                "latency_ms": max(0.0, (self.timer() - started) * 1000),
                "returned_values": None,
                "error": type(exc).__name__,
                "error_detail": "provider request failed",
                "rate_limit": _empty_rate_limit(),
            })
            return finalize_record(record)

        response_at = self.clock()
        latency_ms = max(0.0, (self.timer() - started) * 1000)
        payload = _safe_json(response)
        headers = getattr(response, "headers", {})
        rate_limit = {
            "limit": _header(headers, "X-RateLimit-Limit"),
            "remaining": _header(headers, "X-RateLimit-Remaining"),
            "reset": _header(headers, "X-RateLimit-Reset"),
            "retry_after_seconds": _header(headers, "Retry-After"),
        }
        provider_tier = configured_tier
        if isinstance(payload, dict) and payload.get("current_plan"):
            provider_tier = str(payload["current_plan"]).upper()

        record = self._base_record(
            endpoint, ticker, run_id, provider_tier, request_at, response_at,
        )
        status = _status_for_response(response.status_code, payload)
        record.update({
            "status": status,
            "http_status": int(response.status_code),
            "latency_ms": latency_ms,
            "returned_values": payload if response.status_code == 200 else None,
            "error_payload": payload if response.status_code != 200 else None,
            "error": _provider_error(payload) if response.status_code != 200 else None,
            "rate_limit": rate_limit,
        })
        return finalize_record(record)

    @staticmethod
    def _base_record(
        endpoint: EndpointSpec,
        symbol: str,
        run_id: str,
        configured_tier: str,
        request_at: datetime,
        response_at: datetime,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "module_version": MODULE_VERSION,
            "run_id": run_id,
            "vendor": "FlashAlpha",
            "base_url": BASE_URL,
            "endpoint_name": endpoint.name,
            "endpoint_path": endpoint.path,
            "endpoint_semantics": endpoint.semantics,
            "documented_minimum_tier": endpoint.documented_minimum_tier,
            "account_tier": configured_tier,
            "symbol": symbol,
            "request_at_utc": iso_utc(request_at),
            "response_at_utc": iso_utc(response_at),
            "advisory_only": True,
            "observational_only": True,
            "is_qualifying": False,
            "admission_authority": False,
            "execution_authority": False,
        }


def _empty_rate_limit() -> dict[str, None]:
    return {
        "limit": None,
        "remaining": None,
        "reset": None,
        "retry_after_seconds": None,
    }


def _status_for_response(status_code: int, payload: Any) -> str:
    if status_code == 200:
        return "AVAILABLE" if payload is not None else "MALFORMED_RESPONSE"
    if status_code == 429:
        return "RATE_LIMITED"
    if status_code == 403:
        return "TIER_RESTRICTED"
    if status_code in (401, 404):
        return "UNAVAILABLE"
    if status_code >= 500:
        return "PROVIDER_ERROR"
    return "HTTP_ERROR"


def finalize_record(record: dict[str, Any]) -> dict[str, Any]:
    complete = dict(record)
    complete["record_hash"] = canonical_hash(complete)
    return complete


def append_jsonl(path: Path | str, record: dict[str, Any]) -> None:
    """Append one complete record with O_APPEND and fsync to an isolated ledger."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def normalize_symbol(symbol: str) -> str:
    ticker = str(symbol).strip().upper()
    if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
        raise ValueError(f"invalid symbol: {symbol!r}")
    return ticker


def resolve_endpoints(names: Iterable[str]) -> list[EndpointSpec]:
    resolved = []
    for name in names:
        clean = str(name).strip().lower().replace("-", "_")
        if clean not in ENDPOINTS:
            raise ValueError(f"unknown endpoint {name!r}; choose from {', '.join(ENDPOINTS)}")
        resolved.append(ENDPOINTS[clean])
    if not resolved:
        raise ValueError("at least one endpoint is required")
    return resolved


def run_shadow_fetch(
    *,
    symbols: Iterable[str],
    endpoints: Iterable[EndpointSpec],
    output_path: Path | str = DEFAULT_OUTPUT_PATH,
    call_budget: int = DEFAULT_CALL_BUDGET,
    configured_tier: str = "UNKNOWN",
    key_loader: Callable[[], str] = load_keychain_api_key,
    client_factory: Callable[..., FlashAlphaShadowClient] = FlashAlphaShadowClient,
    clock: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    if call_budget <= 0:
        raise ValueError("call_budget must be positive")
    clean_symbols = [normalize_symbol(symbol) for symbol in symbols]
    clean_endpoints = list(endpoints)
    if not clean_symbols or not clean_endpoints:
        raise ValueError("at least one symbol and endpoint are required")

    run_id = str(uuid.uuid4())
    planned = len(clean_symbols) * len(clean_endpoints)
    summary = {
        "run_id": run_id,
        "output_path": str(output_path),
        "planned_requests": planned,
        "call_budget": int(call_budget),
        "attempted_requests": 0,
        "written_records": 0,
        "stopped_reason": None,
        "advisory_only": True,
        "execution_authority": False,
    }
    try:
        api_key = key_loader()
    except KeychainError as exc:
        record = finalize_record({
            "schema_version": SCHEMA_VERSION,
            "module_version": MODULE_VERSION,
            "run_id": run_id,
            "vendor": "FlashAlpha",
            "endpoint_name": None,
            "endpoint_path": None,
            "account_tier": configured_tier.upper(),
            "symbol": None,
            "request_at_utc": iso_utc(clock()),
            "response_at_utc": iso_utc(clock()),
            "status": "KEYCHAIN_UNAVAILABLE",
            "http_status": None,
            "returned_values": None,
            "error": str(exc),
            "rate_limit": _empty_rate_limit(),
            "advisory_only": True,
            "observational_only": True,
            "is_qualifying": False,
            "admission_authority": False,
            "execution_authority": False,
        })
        append_jsonl(output_path, record)
        summary.update(written_records=1, stopped_reason="KEYCHAIN_UNAVAILABLE")
        return summary

    client = client_factory(api_key)
    for symbol in clean_symbols:
        for endpoint in clean_endpoints:
            if summary["attempted_requests"] >= call_budget:
                summary["stopped_reason"] = "CALL_BUDGET_REACHED"
                return summary
            record = client.fetch(
                endpoint=endpoint,
                symbol=symbol,
                run_id=run_id,
                configured_tier=configured_tier.upper(),
            )
            summary["attempted_requests"] += 1
            append_jsonl(output_path, record)
            summary["written_records"] += 1
            if record["status"] == "RATE_LIMITED":
                summary["stopped_reason"] = "RATE_LIMITED"
                return summary
    summary["stopped_reason"] = "COMPLETE"
    return summary


def _csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only FlashAlpha shadow-signal logger",
    )
    parser.add_argument("--symbols", default="SPY", help="comma-separated symbols")
    parser.add_argument(
        "--endpoints", default="levels",
        help=f"comma-separated endpoint names: {', '.join(ENDPOINTS)}",
    )
    parser.add_argument("--budget", type=int, default=DEFAULT_CALL_BUDGET)
    parser.add_argument("--tier", default="UNKNOWN", help="account tier provenance stamp")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--keychain-account", default=getpass.getuser())
    args = parser.parse_args()
    endpoint_specs = resolve_endpoints(_csv_values(args.endpoints))
    summary = run_shadow_fetch(
        symbols=_csv_values(args.symbols),
        endpoints=endpoint_specs,
        output_path=args.output,
        call_budget=args.budget,
        configured_tier=args.tier,
        key_loader=lambda: load_keychain_api_key(account=args.keychain_account),
    )
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0 if summary["stopped_reason"] in {"COMPLETE", "CALL_BUDGET_REACHED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
