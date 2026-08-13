#!/usr/bin/env python3
"""Bounded, read-only IVolatility entitlement and schema validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from options_intelligence import (
    build_chain_snapshot, canonical_hash, engineer_options_features,
    normalize_ivolatility_row,
)


BASE_URL = "https://restapi.ivolatility.com"
REPORT_VERSION = "ivolatility-entitlement-validation-v0"
DEFAULT_OUTPUT = Path("v2_data/options/validation/latest.json")


def recent_weekday():
    day = date.today() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def _rows(payload):
    value = payload.get("data", payload) if isinstance(payload, dict) else payload
    return value if isinstance(value, list) else []


def _hash(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()


def _status(http_status, payload):
    if http_status == 200:
        code = str((payload.get("status") or {}).get("code") or "").upper() \
            if isinstance(payload, dict) else ""
        return code if code in {"PENDING", "ERROR"} else "AVAILABLE"
    return {
        204: "NO_DATA", 400: "INVALID_PARAMETERS", 401: "UNAUTHENTICATED",
        403: "FORBIDDEN", 429: "RATE_LIMITED",
    }.get(http_status, "TRANSIENT_ERROR" if http_status >= 500 else "ERROR")


def probe(session, *, name, path, params, key, timeout=8.0):
    request_params = dict(params or {})
    request_params["apiKey"] = key
    started = time.monotonic()
    try:
        response = session.get(BASE_URL + path, params=request_params, timeout=timeout)
    except requests.RequestException:
        return {
            "name": name, "path": path, "status": "NETWORK_ERROR",
            "http_status": None,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "record_count": None, "field_names": [], "payload": None,
        }
    latency = round((time.monotonic() - started) * 1000, 1)
    payload = None
    if response.status_code == 200:
        try:
            payload = response.json()
        except ValueError:
            return {
                "name": name, "path": path, "status": "INVALID_JSON",
                "http_status": 200, "latency_ms": latency,
                "record_count": None, "field_names": [], "payload": None,
            }
    rows = _rows(payload)
    fields = sorted({str(field) for row in rows[:10]
                     if isinstance(row, dict) for field in row})
    return {
        "name": name, "path": path,
        "status": _status(response.status_code, payload),
        "http_status": response.status_code, "latency_ms": latency,
        "record_count": len(rows) if payload is not None else None,
        "field_names": fields, "payload": payload,
    }


def validate(session, *, key, trade_date=None, timeout=8.0, now=None,
             spacing_seconds=1.1):
    trade_date = trade_date or recent_weekday()
    now = now or datetime.now(timezone.utc)
    specs = [
        ("chain_calls", "/equities/eod/stock-opts-by-param", {
            "symbol": "SPY", "tradeDate": trade_date, "dteFrom": 0,
            "dteTo": 2, "moneynessFrom": -10, "moneynessTo": 10,
            "cp": "C", "region": "USA"}),
        ("chain_puts", "/equities/eod/stock-opts-by-param", {
            "symbol": "SPY", "tradeDate": trade_date, "dteFrom": 0,
            "dteTo": 2, "moneynessFrom": -10, "moneynessTo": 10,
            "cp": "P", "region": "USA"}),
        ("stock_market_data", "/equities/stock-market-data", {
            "symbols": "SPY", "from": trade_date, "to": trade_date}),
        ("historical_volatility", "/equities/eod/hv", {
            "symbol": "SPY", "date": trade_date}),
        ("iv_surface", "/equities/eod/ivs", {
            "symbol": "SPY", "date": trade_date}),
        ("iv_index", "/equities/eod/ivx", {
            "symbol": "SPY", "date": trade_date}),
    ]
    results = []
    raw_by_name = {}
    for name, path, params in specs:
        result = probe(
            session, name=name, path=path, params=params, key=key,
            timeout=timeout)
        raw_by_name[name] = result.pop("payload")
        results.append(result)
        time.sleep(max(0.0, spacing_seconds))

    chain_rows = _rows(raw_by_name.get("chain_calls")) + _rows(raw_by_name.get("chain_puts"))
    contracts, rejected = [], []
    for index, row in enumerate(chain_rows):
        try:
            contracts.append(normalize_ivolatility_row(row, underlying="SPY"))
        except (TypeError, ValueError) as exc:
            rejected.append({"row": index, "reason": str(exc)[:160]})

    if contracts:
        first = contracts[0]
        option_type = "CALL" if first.right == "C" else "PUT"
        contract_params = {
            "symbol": "SPY", "date": trade_date,
            "expDate": first.expiration_date, "strike": first.strike,
            "optType": option_type, "minuteType": "MINUTE_15",
        }
        for name, path, params in (
            ("intraday_contract", "/equities/intraday/single-equity-option-rawiv", contract_params),
            ("delayed_contract", "/equities/dl/options-rawiv", {"symbols": first.option_symbol}),
            ("realtime_contract", "/equities/rt/options-rawiv", {"symbols": first.option_symbol}),
        ):
            result = probe(
                session, name=name, path=path, params=params, key=key,
                timeout=timeout)
            raw_by_name[name] = result.pop("payload")
            results.append(result)
            time.sleep(max(0.0, spacing_seconds))

    captured_at = now.isoformat()
    raw_capture = {
        "provider": "ivolatility", "trade_date": trade_date,
        "chain_calls": raw_by_name.get("chain_calls"),
        "chain_puts": raw_by_name.get("chain_puts"),
    }
    snapshot = build_chain_snapshot(
        provider="ivolatility", underlying="SPY", captured_at=captured_at,
        contracts=contracts, raw_payload_hash=canonical_hash(raw_capture),
        source_status="ready" if contracts and not rejected else "degraded",
    )
    features_a = engineer_options_features(snapshot)
    features_b = engineer_options_features(snapshot)
    coverage = {}
    for field in ("bid", "ask", "volume", "open_interest", "iv", "delta",
                  "gamma", "theta", "vega", "underlying_price"):
        coverage[field] = sum(getattr(item, field) is not None for item in contracts)
    sanitized = {
        "report_version": REPORT_VERSION, "validated_at": captured_at,
        "trade_date": trade_date, "ticker": "SPY",
        "request_count": len(results), "endpoints": results,
        "normalization": {
            "input_contracts": len(chain_rows),
            "normalized_contracts": len(contracts),
            "rejected_contracts": len(rejected),
            "rejection_reasons": rejected[:10], "field_coverage": coverage,
        },
        "replay": {
            "deterministic": features_a == features_b,
            "snapshot_hash": snapshot["snapshot_id"],
            "feature_hash": features_a["feature_id"],
            "qualifying": False, "recommendation": None,
        },
        "execution_authority": False, "credential_persisted": False,
    }
    return sanitized, raw_capture


def _write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--trade-date")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--spacing", type=float, default=1.1)
    args = parser.parse_args()
    key = (os.getenv("IVOLATILITY_API_KEY") or "").strip()
    if not key:
        print("IVolatility validation: SKIPPED (IVOLATILITY_API_KEY unavailable)")
        return 2
    session = requests.Session()
    report, raw_capture = validate(
        session, key=key, trade_date=args.trade_date, timeout=args.timeout,
        spacing_seconds=args.spacing)
    raw_path = args.output.with_name(args.output.stem + "-private-sample.json")
    _write_private(raw_path, raw_capture)
    report["private_sample"] = {
        "path": str(raw_path), "content_hash": _hash(raw_capture),
        "redistribution": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(f"IVolatility validation: {report['request_count']} read-only probes")
    for endpoint in report["endpoints"]:
        print(f"  {endpoint['name']}: {endpoint['status']} "
              f"(HTTP {endpoint['http_status']}, {endpoint['latency_ms']} ms, "
              f"rows={endpoint['record_count']})")
    normalized = report["normalization"]
    print(f"Normalized contracts: {normalized['normalized_contracts']}/"
          f"{normalized['input_contracts']} (rejected={normalized['rejected_contracts']})")
    print(f"Deterministic replay: {report['replay']['deterministic']}")
    print(f"Sanitized report: {args.output}")
    print(f"Private sample: {raw_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
