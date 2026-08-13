#!/usr/bin/env python3
"""Bounded, read-only Unusual Whales entitlement check and replay capture.

The token is accepted only through UW_API_KEY. It is never written, printed,
hashed into evidence, or included in errors and reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from institutional_flow import aggregate_flow_events, normalize_unusual_whales_alert


BASE_URL = "https://api.unusualwhales.com/api"
REPORT_VERSION = "uw-entitlement-validation-v0"
DEFAULT_OUTPUT = Path("v2_data/institutional_flow/validation/latest.json")
PROBES = (
    ("stock_state", "stock/SPY/stock-state", None),
    ("flow_alerts", "stock/SPY/flow-alerts", {"limit": 100}),
    ("darkpool", "darkpool/SPY", {"limit": 10}),
    ("gex_levels", "stock/SPY/gex-levels", None),
    ("iv_rank", "stock/SPY/iv-rank", None),
    ("expiry_breakdown", "stock/SPY/expiry-breakdown", None),
    ("term_structure", "stock/SPY/volatility/term-structure", None),
)


def _hash(value):
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def _classification(status_code):
    return {
        200: "AVAILABLE", 204: "NO_DATA", 401: "UNAUTHENTICATED",
        403: "FORBIDDEN", 429: "RATE_LIMITED",
    }.get(status_code, "TRANSIENT_ERROR" if status_code >= 500 else "ERROR")


def _rows(payload):
    value = payload.get("data", payload) if isinstance(payload, dict) else payload
    return value if isinstance(value, list) else []


def probe_endpoint(session, name, path, params, *, timeout=5.0):
    started = time.monotonic()
    try:
        response = session.get(
            f"{BASE_URL}/{path}", params=params, timeout=timeout)
    except requests.RequestException:
        return {
            "name": name, "status": "NETWORK_ERROR", "http_status": None,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "record_count": None, "payload": None,
        }
    latency = round((time.monotonic() - started) * 1000, 1)
    payload = None
    if response.status_code == 200:
        try:
            payload = response.json()
        except ValueError:
            return {
                "name": name, "status": "INVALID_JSON",
                "http_status": 200, "latency_ms": latency,
                "record_count": None, "payload": None,
            }
    return {
        "name": name,
        "status": _classification(response.status_code),
        "http_status": response.status_code,
        "latency_ms": latency,
        "record_count": len(_rows(payload)) if payload is not None else None,
        "payload": payload,
    }


def build_controlled_replay(raw_rows, *, received_at):
    events, rejected = [], 0
    rejection_reasons = {}
    field_names = sorted({str(key) for row in raw_rows[:100]
                          if isinstance(row, dict) for key in row})
    for row in raw_rows[:100]:
        if not isinstance(row, dict):
            rejected += 1
            continue
        try:
            events.append(normalize_unusual_whales_alert(row, received_at=received_at))
        except (TypeError, ValueError) as exc:
            rejected += 1
            reason = str(exc)[:120]
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
    unique = {event.dedupe_key: event for event in events}
    ordered = sorted(unique.values(), key=lambda event: (event.observed_at, event.dedupe_key))
    # Receipt time is the replay decision boundary. No provider event received
    # after it is allowed into the feature snapshot.
    eligible = [event for event in ordered if event.received_at <= received_at]
    snapshots_a = [aggregate_flow_events(
        eligible, ticker="SPY", window_minutes=window,
        window_end=received_at, provider_available=True)
        for window in (1, 5, 15, 30)]
    snapshots_b = [aggregate_flow_events(
        eligible, ticker="SPY", window_minutes=window,
        window_end=received_at, provider_available=True)
        for window in (1, 5, 15, 30)]
    sample_window_minutes = 1
    if eligible:
        oldest_age = max(0.0, (received_at - min(event.observed_at for event in eligible)).total_seconds())
        sample_window_minutes = min(7 * 24 * 60, max(1, math.ceil(oldest_age / 60) + 1))
    sample_a = aggregate_flow_events(
        eligible, ticker="SPY", window_minutes=sample_window_minutes,
        window_end=received_at, provider_available=True)
    sample_b = aggregate_flow_events(
        eligible, ticker="SPY", window_minutes=sample_window_minutes,
        window_end=received_at, provider_available=True)
    dump_a = [item.model_dump(mode="json") for item in snapshots_a]
    dump_b = [item.model_dump(mode="json") for item in snapshots_b]
    event_dump = [event.model_dump(mode="json") for event in eligible]
    sample_dump = sample_a.model_dump(mode="json")
    return {
        "input_rows": len(raw_rows[:100]),
        "normalized_rows": len(events),
        "rejected_rows": rejected,
        "rejection_reasons": rejection_reasons,
        "input_field_names": field_names,
        "unique_events": len(eligible),
        "duplicates_removed": len(events) - len(unique),
        "decision_time": received_at.isoformat(),
        "lookahead_violations": sum(event.received_at > received_at for event in eligible),
        "event_hash": _hash(event_dump),
        "snapshot_hash": _hash({"rolling": dump_a, "sample": sample_dump}),
        "deterministic": (dump_a == dump_b and sample_a == sample_b
                          and _hash(dump_a) == _hash(dump_b)),
        "snapshots": dump_a,
        "fixed_sample_window_minutes": sample_window_minutes,
        "fixed_sample_event_count": sample_a.event_count,
        "fixed_sample_snapshot": sample_dump,
    }


def validate(session, *, now=None, timeout=5.0):
    now = now or datetime.now(timezone.utc)
    results = []
    flow_rows = []
    for name, path, params in PROBES:
        result = probe_endpoint(session, name, path, params, timeout=timeout)
        if name == "flow_alerts" and result["status"] == "AVAILABLE":
            flow_rows = _rows(result["payload"])
        results.append({key: value for key, value in result.items() if key != "payload"})
        time.sleep(0.15)
    return {
        "report_version": REPORT_VERSION,
        "validated_at": now.isoformat(),
        "ticker": "SPY",
        "request_count": len(PROBES),
        "endpoints": results,
        "streaming": {
            "status": "NOT_TESTED",
            "reason": "Kafka/WebSocket entitlement requires a separately documented connection test",
        },
        "controlled_replay": build_controlled_replay(flow_rows, received_at=now),
        "execution_authority": False,
        "credential_persisted": False,
        "_private_flow_rows": flow_rows,
    }


def _write_private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    token = (os.getenv("UW_API_KEY") or "").strip()
    if not token:
        print("UW entitlement validation: SKIPPED (UW_API_KEY unavailable)")
        return 2
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "User-Agent": "Scalpr-UW-Entitlement-Validation/0",
    })
    report = validate(session, timeout=args.timeout)
    private_rows = report.pop("_private_flow_rows")
    sample_path = args.output.with_name(args.output.stem + "-flow-sample.json")
    _write_private_json(sample_path, private_rows)
    report["private_replay_sample"] = {
        "path": str(sample_path), "rows": len(private_rows),
        "content_hash": _hash(private_rows), "redistribution": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(f"UW entitlement validation: {report['request_count']} read-only probes")
    for endpoint in report["endpoints"]:
        print(f"  {endpoint['name']}: {endpoint['status']} "
              f"(HTTP {endpoint['http_status']}, {endpoint['latency_ms']} ms)")
    replay = report["controlled_replay"]
    print(f"Controlled replay: {replay['unique_events']} unique events, "
          f"deterministic={replay['deterministic']}, "
          f"lookahead_violations={replay['lookahead_violations']}")
    print(f"Sanitized report: {args.output}")
    print(f"Private replay sample: {sample_path} ({len(private_rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
