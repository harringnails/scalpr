"""Network-free tests for isolated FlashAlpha shadow ingestion."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import requests

import flashalpha_shadow_v0 as shadow


NOW = datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc)


class Response:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, ValueError):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def clock():
    return NOW


def timer():
    return 1.0


def records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def client_factory(session):
    return lambda key: shadow.FlashAlphaShadowClient(
        key, session=session, clock=clock, timer=timer,
    )


def test_keychain_lookup_uses_service_without_exposing_key_in_command():
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="fixture-secret\n", stderr="")

    key = shadow.load_keychain_api_key(account="operator", runner=runner)
    assert key == "fixture-secret"
    assert captured["command"] == [
        "security", "find-generic-password", "-a", "operator",
        "-s", "scalpr.flashalpha.api", "-w",
    ]
    assert "fixture-secret" not in " ".join(captured["command"])


def test_confirmed_endpoint_registry_uses_documented_paths():
    assert shadow.ENDPOINTS["gex"].path == "/exposure/gex/{symbol}"
    assert shadow.ENDPOINTS["levels"].path == "/exposure/levels/{symbol}"
    assert shadow.ENDPOINTS["maxpain"].path == "/maxpain/{symbol}"
    assert shadow.ENDPOINTS["zero_dte"].path == "/exposure/zero-dte/{symbol}"
    assert shadow.ENDPOINTS["flow_pin_risk"].path == "/flow/pin-risk/{symbol}"


def test_success_is_point_in_time_provenance_stamped_and_hashed(tmp_path):
    session = Session([Response(200, {
        "symbol": "SPY", "levels": {"gamma_flip": 600, "call_wall": 610},
    }, headers={
        "X-RateLimit-Limit": "250", "X-RateLimit-Remaining": "249",
    })])
    output = tmp_path / "flashalpha_shadow_v0.jsonl"
    summary = shadow.run_shadow_fetch(
        symbols=["SPY"], endpoints=[shadow.ENDPOINTS["levels"]],
        output_path=output, configured_tier="Basic", key_loader=lambda: "key",
        client_factory=client_factory(session), clock=clock,
    )
    row = records(output)[0]
    assert summary["attempted_requests"] == 1
    assert session.calls[0][0] == "https://lab.flashalpha.com/v1/exposure/levels/SPY"
    assert session.headers["X-Api-Key"] == "key"
    assert row["status"] == "AVAILABLE"
    assert row["request_at_utc"] == NOW.isoformat()
    assert row["response_at_utc"] == NOW.isoformat()
    assert row["account_tier"] == "BASIC"
    assert row["returned_values"]["levels"]["gamma_flip"] == 600
    assert row["execution_authority"] is False
    assert row["admission_authority"] is False
    expected = dict(row)
    expected.pop("record_hash")
    assert row["record_hash"] == shadow.canonical_hash(expected)


def test_tier_restriction_is_recorded_without_fabricated_values(tmp_path):
    session = Session([Response(403, {
        "error": "tier_restricted", "current_plan": "Free",
        "required_plan": "Basic", "message": "ETF data requires Basic",
    })])
    output = tmp_path / "flashalpha_shadow_v0.jsonl"
    shadow.run_shadow_fetch(
        symbols=["SPY"], endpoints=[shadow.ENDPOINTS["gex"]],
        output_path=output, configured_tier="UNKNOWN", key_loader=lambda: "key",
        client_factory=client_factory(session), clock=clock,
    )
    row = records(output)[0]
    assert row["status"] == "TIER_RESTRICTED"
    assert row["account_tier"] == "FREE"
    assert row["returned_values"] is None
    assert row["error_payload"]["required_plan"] == "Basic"


def test_rate_limit_record_is_written_and_stops_remaining_requests(tmp_path):
    session = Session([
        Response(429, {
            "error": "Quota exceeded", "current_plan": "Free", "limit": 5,
            "reset_at": "2026-09-01T00:00:00Z",
        }, headers={"Retry-After": "3600", "X-RateLimit-Remaining": "0"}),
        Response(200, {"must_not": "be_requested"}),
    ])
    output = tmp_path / "flashalpha_shadow_v0.jsonl"
    summary = shadow.run_shadow_fetch(
        symbols=["SPY"],
        endpoints=[shadow.ENDPOINTS["levels"], shadow.ENDPOINTS["maxpain"]],
        output_path=output, call_budget=2, configured_tier="Free",
        key_loader=lambda: "key", client_factory=client_factory(session), clock=clock,
    )
    row = records(output)[0]
    assert summary["stopped_reason"] == "RATE_LIMITED"
    assert summary["attempted_requests"] == 1
    assert len(session.calls) == 1
    assert row["status"] == "RATE_LIMITED"
    assert row["returned_values"] is None
    assert row["rate_limit"]["retry_after_seconds"] == "3600"


def test_default_budget_allows_only_one_request(tmp_path):
    session = Session([
        Response(200, {"first": True}), Response(200, {"second": True}),
    ])
    output = tmp_path / "flashalpha_shadow_v0.jsonl"
    summary = shadow.run_shadow_fetch(
        symbols=["SPY"],
        endpoints=[shadow.ENDPOINTS["levels"], shadow.ENDPOINTS["maxpain"]],
        output_path=output, key_loader=lambda: "key",
        client_factory=client_factory(session), clock=clock,
    )
    assert shadow.DEFAULT_CALL_BUDGET == 1
    assert summary["stopped_reason"] == "CALL_BUDGET_REACHED"
    assert len(session.calls) == 1
    assert len(records(output)) == 1


def test_network_failure_and_malformed_json_remain_explicit(tmp_path):
    session = Session([
        requests.ConnectionError("fixture"),
        Response(200, ValueError("not json")),
    ])
    output = tmp_path / "flashalpha_shadow_v0.jsonl"
    summary = shadow.run_shadow_fetch(
        symbols=["SPY"],
        endpoints=[shadow.ENDPOINTS["gex"], shadow.ENDPOINTS["levels"]],
        output_path=output, call_budget=2, key_loader=lambda: "key",
        client_factory=client_factory(session), clock=clock,
    )
    rows = records(output)
    assert summary["attempted_requests"] == 2
    assert rows[0]["status"] == "REQUEST_ERROR"
    assert rows[0]["returned_values"] is None
    assert rows[1]["status"] == "MALFORMED_RESPONSE"
    assert rows[1]["returned_values"] is None


def test_missing_key_is_logged_without_http_call(tmp_path):
    output = tmp_path / "flashalpha_shadow_v0.jsonl"

    def missing():
        raise shadow.KeychainError("fixture missing")

    summary = shadow.run_shadow_fetch(
        symbols=["SPY"], endpoints=[shadow.ENDPOINTS["levels"]],
        output_path=output, key_loader=missing, clock=clock,
    )
    row = records(output)[0]
    assert summary["attempted_requests"] == 0
    assert summary["stopped_reason"] == "KEYCHAIN_UNAVAILABLE"
    assert row["status"] == "KEYCHAIN_UNAVAILABLE"
    assert row["returned_values"] is None


def test_append_is_repeatable_and_never_rewrites_existing_rows(tmp_path):
    output = tmp_path / "flashalpha_shadow_v0.jsonl"
    first = shadow.finalize_record({"n": 1})
    second = shadow.finalize_record({"n": 2})
    shadow.append_jsonl(output, first)
    before = output.read_bytes()
    shadow.append_jsonl(output, second)
    assert output.read_bytes().startswith(before)
    assert records(output) == [first, second]


def test_module_import_graph_has_no_execution_or_existing_store_paths():
    forbidden = {
        "scalp_server", "guard_events", "entry_policy", "entry_intelligence_v1",
        "entry_cohort_lock_v1", "wave_order_adapter", "operational_store",
        "institutional_flow_store", "a2_measurement", "options_feature_store",
    }
    tree = ast.parse(Path(shadow.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(forbidden)
    source = Path(shadow.__file__).read_text()
    assert "flashalpha_shadow_v0.jsonl" in source
    assert "scalp.keys" not in source
