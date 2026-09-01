"""Network-free tests for the FlashAlpha SPY pin-candidate shadow study."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import flashalpha_pin_scanner_v0 as scanner
import flashalpha_shadow_v0 as shadow


OBSERVED = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)  # 15:00 ET


def raw_record(endpoint, payload=None, *, status="AVAILABLE", observed=OBSERVED):
    row = {
        "schema_version": shadow.SCHEMA_VERSION,
        "module_version": shadow.MODULE_VERSION,
        "run_id": "fixture-run",
        "vendor": "FlashAlpha",
        "base_url": shadow.BASE_URL,
        "endpoint_name": endpoint,
        "endpoint_path": shadow.ENDPOINTS[endpoint].path,
        "endpoint_semantics": shadow.ENDPOINTS[endpoint].semantics,
        "documented_minimum_tier": shadow.ENDPOINTS[endpoint].documented_minimum_tier,
        "account_tier": "GROWTH",
        "symbol": "SPY",
        "request_at_utc": observed.isoformat(),
        "response_at_utc": observed.isoformat(),
        "status": status,
        "http_status": 200 if status == "AVAILABLE" else 403,
        "returned_values": payload if status == "AVAILABLE" else None,
        "error_payload": None if status == "AVAILABLE" else {"error": status},
        "rate_limit": {"limit": "2500", "remaining": "2499", "reset": None,
                       "retry_after_seconds": None},
        "advisory_only": True,
        "observational_only": True,
        "is_qualifying": False,
        "admission_authority": False,
        "execution_authority": False,
    }
    return shadow.finalize_record(row)


def payloads(*, regime="positive_gamma", pin_score=75, hours=1.0,
             as_of="2026-08-31T18:59:30Z", spot=600.0):
    gex = {
        "symbol": "SPY", "underlying_price": spot, "as_of": as_of,
        "gamma_flip": 598.0, "net_gex": 2_000_000_000,
        "net_gex_label": "positive",
        "strikes": [
            {"strike": 590, "call_oi": 100, "put_oi": 900,
             "call_gex": 10, "put_gex": -900, "net_gex": -890},
            {"strike": 600, "call_oi": 800, "put_oi": 850,
             "call_gex": 700, "put_gex": -650, "net_gex": 50},
            {"strike": 605, "call_oi": 600, "put_oi": 200,
             "call_gex": 500, "put_gex": -100, "net_gex": 400},
            {"strike": 610, "call_oi": 1000, "put_oi": 100,
             "call_gex": 1000, "put_gex": -50, "net_gex": 950},
        ],
    }
    levels = {
        "symbol": "SPY", "underlying_price": spot, "as_of": as_of,
        "levels": {
            "gamma_flip": 598.0, "call_wall": 610.0, "put_wall": 590.0,
            "zero_dte_magnet": 600.0, "highest_oi_strike": 600.0,
        },
    }
    zero_dte = {
        "symbol": "SPY", "underlying_price": spot, "as_of": as_of,
        "time_to_close_hours": hours,
        "regime": {"label": regime, "gamma_flip": 599.0},
        "exposures": {"pct_of_total_gex": 62.5},
        "pin_risk": {"pin_score": pin_score, "magnet_strike": 600.0, "max_pain": 600.0},
        "levels": {"call_wall": 605.0, "put_wall": 595.0, "zero_dte_magnet": 600.0},
        "strikes": [
            {"strike": 595, "call_oi": 250, "put_oi": 800,
             "call_gex": 80, "put_gex": -700, "net_gex": -620},
            {"strike": 600, "call_oi": 700, "put_oi": 750,
             "call_gex": 600, "put_gex": -550, "net_gex": 50},
            {"strike": 605, "call_oi": 900, "put_oi": 200,
             "call_gex": 850, "put_gex": -100, "net_gex": 750},
        ],
    }
    return gex, levels, zero_dte


def raw_set(**kwargs):
    gex, levels, zero_dte = payloads(**kwargs)
    return [raw_record("gex", gex), raw_record("levels", levels), raw_record("zero_dte", zero_dte)]


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def test_structure_map_and_high_candidate_are_data_derived():
    candidate = scanner.build_candidate(raw_set(), observed_at=OBSERVED)
    repeated = scanner.build_candidate(raw_set(), observed_at=OBSERVED)
    evidence = candidate["evidence"]
    assert candidate["grade"] == "HIGH_PIN_PRESSURE"
    assert evidence["gamma_regime"] == "positive"
    assert evidence["call_wall"] == {
        "price": 605.0, "source": "zero_dte.levels", "field": "call_wall",
    }
    assert evidence["put_wall"] == {
        "price": 595.0, "source": "zero_dte.levels", "field": "put_wall",
    }
    assert evidence["pocket"]["width"] == 10.0
    assert evidence["pocket"]["spot_normalized_position"] == 0.5
    assert evidence["spot_to_call_wall_bps"] == pytest.approx(83.3333333)
    assert evidence["spot_to_put_wall_bps"] == pytest.approx(83.3333333)
    assert evidence["zero_dte_gex_share_pct"] == 62.5
    assert evidence["max_pain_magnet_agree"] is True
    assert evidence["median_strike_spacing"] == 5.0
    at_600 = next(row for row in evidence["strike_map"] if row["strike"] == 600)
    assert at_600["full_chain_call_oi"] == 800
    assert at_600["zero_dte_call_oi"] == 700
    assert at_600["call_oi"] == 700
    assert candidate["is_qualifying"] is False
    assert candidate["execution_authority"] is False
    assert candidate == repeated


def test_negative_gamma_is_anti_pin_even_when_another_endpoint_is_restricted():
    rows = raw_set(regime="negative_gamma")
    rows[0] = raw_record("gex", status="TIER_RESTRICTED")
    candidate = scanner.build_candidate(rows, observed_at=OBSERVED)
    assert candidate["grade"] == "ANTI_PIN_NEGATIVE_GAMMA"
    assert candidate["grade_reasons"] == ["NEGATIVE_GAMMA_BREAKOUT_GATE"]


@pytest.mark.parametrize(
    ("score", "hours", "expected"),
    [
        (39, 1.0, "LOW_PIN_PRESSURE"),
        (40, 1.0, "MODERATE_PIN_PRESSURE"),
        (69, 1.0, "MODERATE_PIN_PRESSURE"),
        (70, 2.0, "HIGH_PIN_PRESSURE"),
        (90, 2.5, "MODERATE_PIN_PRESSURE"),
    ],
)
def test_provider_score_bands_and_final_two_hour_cap(score, hours, expected):
    candidate = scanner.build_candidate(
        raw_set(pin_score=score, hours=hours), observed_at=OBSERVED,
    )
    assert candidate["grade"] == expected


def test_basic_tier_and_stale_data_degrade_to_unknown():
    gex, levels, _ = payloads()
    basic = [
        raw_record("gex", gex), raw_record("levels", levels),
        raw_record("zero_dte", status="TIER_RESTRICTED"),
    ]
    basic_candidate = scanner.build_candidate(basic, observed_at=OBSERVED)
    assert basic_candidate["grade"] == "UNKNOWN"
    assert "pin_score" in basic_candidate["evidence"]["missing_required_evidence"]

    stale = scanner.build_candidate(
        raw_set(as_of="2026-08-31T18:40:00Z"), observed_at=OBSERVED,
    )
    assert stale["grade"] == "UNKNOWN"
    assert stale["evidence"]["data_freshness"]["status"] == "UNAVAILABLE_OR_STALE"


class Response:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def client_factory(session):
    return lambda key: shadow.FlashAlphaShadowClient(
        key, session=session, clock=lambda: OBSERVED, timer=lambda: 1.0,
    )


def test_scan_once_logs_three_raw_records_and_one_candidate(tmp_path):
    gex, levels, zero_dte = payloads()
    session = Session([Response(200, gex), Response(200, levels), Response(200, zero_dte)])
    raw_path = tmp_path / "raw.jsonl"
    study_path = tmp_path / "study.jsonl"
    summary = scanner.scan_once(
        raw_path=raw_path, study_path=study_path, key_loader=lambda: "fixture-key",
        client_factory=client_factory(session), clock=lambda: OBSERVED,
    )
    assert summary["grade"] == "HIGH_PIN_PRESSURE"
    assert summary["raw_records_written"] == 3
    assert summary["http_requests_attempted"] == 3
    assert len(session.calls) == 3
    assert len(read_rows(raw_path)) == 3
    assert read_rows(study_path)[0]["record_type"] == "PIN_CANDIDATE"


def test_429_stops_endpoint_sequence_and_poll_loop(tmp_path):
    session = Session([Response(429, {"error": "Quota exceeded"})])
    sleeps = []
    result = scanner.poll(
        polls=3, interval_seconds=300, sleep=sleeps.append,
        raw_path=tmp_path / "raw.jsonl", study_path=tmp_path / "study.jsonl",
        key_loader=lambda: "fixture-key", client_factory=client_factory(session),
        clock=lambda: OBSERVED,
    )
    assert result == {"polls_completed": 1, "stopped_reason": "RATE_LIMITED"}
    assert len(session.calls) == 1
    assert sleeps == []


def test_declared_basic_and_free_tiers_do_not_burn_restricted_calls(tmp_path):
    gex, levels, _ = payloads()
    basic_session = Session([Response(200, gex), Response(200, levels)])
    basic = scanner.scan_once(
        configured_tier="BASIC", raw_path=tmp_path / "basic-raw.jsonl",
        study_path=tmp_path / "basic-study.jsonl", key_loader=lambda: "fixture-key",
        client_factory=client_factory(basic_session), clock=lambda: OBSERVED,
    )
    assert basic["http_requests_attempted"] == 2
    basic_rows = read_rows(tmp_path / "basic-raw.jsonl")
    assert next(row for row in basic_rows if row["endpoint_name"] == "zero_dte")["status"] == "TIER_SKIPPED"

    key_calls = []
    free = scanner.scan_once(
        configured_tier="FREE", raw_path=tmp_path / "free-raw.jsonl",
        study_path=tmp_path / "free-study.jsonl",
        key_loader=lambda: key_calls.append(True), clock=lambda: OBSERVED,
    )
    assert free["http_requests_attempted"] == 0
    assert key_calls == []
    assert {row["status"] for row in read_rows(tmp_path / "free-raw.jsonl")} == {"TIER_SKIPPED"}


def test_outcome_uses_canonical_intraday_candidate_and_fresh_close(tmp_path):
    study_path = tmp_path / "study.jsonl"
    intraday = scanner.build_candidate(raw_set(), observed_at=OBSERVED)
    close_observed = datetime(2026, 8, 31, 20, 0, 15, tzinfo=timezone.utc)
    close_rows = raw_set(
        hours=0.0, as_of="2026-08-31T20:00:05Z", spot=600.4,
    )
    close_candidate = scanner.build_candidate(close_rows, observed_at=close_observed)
    shadow.append_jsonl(study_path, intraday)
    shadow.append_jsonl(study_path, close_candidate)

    outcome = scanner.score_session_outcome("2026-08-31", study_path=study_path)
    assert outcome["status"] == "AVAILABLE"
    assert outcome["selected_candidate_grade"] == "HIGH_PIN_PRESSURE"
    assert outcome["close_price"] == 600.4
    assert outcome["near_magnet_tolerance"] == 2.5
    assert outcome["close_near_magnet"] is True
    assert outcome["close_inside_pocket"] is True
    assert outcome["record_appended"] is True
    duplicate = scanner.score_session_outcome("2026-08-31", study_path=study_path)
    assert duplicate["record_appended"] is False


def outcome_row(session_date, grade, hit, pocket=True):
    row = {
        "schema_version": scanner.OUTCOME_SCHEMA_VERSION,
        "record_type": "PIN_SESSION_OUTCOME",
        "session_date": session_date,
        "status": "AVAILABLE",
        "selected_candidate_grade": grade,
        "close_near_magnet": hit,
        "close_inside_pocket": pocket,
        "is_inferential": False,
        "execution_authority": False,
    }
    row["record_hash"] = shadow.canonical_hash(row)
    return row


def test_report_measures_lift_over_all_and_non_high_base_with_deterministic_null(tmp_path):
    study_path = tmp_path / "study.jsonl"
    rows = [
        outcome_row("2026-08-03", "HIGH_PIN_PRESSURE", True),
        outcome_row("2026-08-04", "HIGH_PIN_PRESSURE", True),
        outcome_row("2026-08-05", "MODERATE_PIN_PRESSURE", False),
        outcome_row("2026-08-06", "ANTI_PIN_NEGATIVE_GAMMA", False),
    ]
    for row in rows:
        shadow.append_jsonl(study_path, row)
    report_a = scanner.build_report(
        study_path=study_path, target_days=20, permutations=199,
        clock=lambda: OBSERVED,
    )
    report_b = scanner.build_report(
        study_path=study_path, target_days=20, permutations=199,
        clock=lambda: OBSERVED,
    )
    assert report_a == report_b
    assert report_a["available_session_days"] == 4
    assert report_a["high_candidate_close_near_magnet_rate"] == 1.0
    assert report_a["all_day_close_near_magnet_base_rate"] == 0.5
    assert report_a["non_high_close_near_magnet_base_rate"] == 0.0
    assert report_a["lift_over_all_day_base_rate"] == 0.5
    assert report_a["lift_over_non_high_base_rate"] == 1.0
    assert report_a["session_block_matched_null"]["finite_sample_plus_one"] is True
    assert report_a["study_status"] == "ACCUMULATING - NON-INFERENTIAL"
    assert report_a["is_inferential"] is False


def test_report_without_high_or_non_high_control_refuses_null(tmp_path):
    study_path = tmp_path / "study.jsonl"
    shadow.append_jsonl(
        study_path, outcome_row("2026-08-03", "MODERATE_PIN_PRESSURE", True),
    )
    report = scanner.build_report(study_path=study_path, clock=lambda: OBSERVED)
    assert report["high_candidate_close_near_magnet_rate"] is None
    assert report["session_block_matched_null"]["available"] is False


def test_scanner_import_graph_is_isolated_and_has_no_synthetic_floor():
    forbidden = {
        "scalp_server", "guard_events", "entry_policy", "entry_intelligence_v1",
        "entry_cohort_lock_v1", "wave_order_adapter", "operational_store",
        "institutional_flow_store", "a2_measurement", "options_feature_store",
    }
    tree = ast.parse(Path(scanner.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(forbidden)
    source = Path(scanner.__file__).read_text().lower()
    assert "dark pool support floor" not in source
    assert '"execution_authority"' in source
