"""Network-free tests for the frozen SPY pin iron-condor shadow study."""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import flashalpha_pin_ic_shadow_v0 as ic
import flashalpha_shadow_v0 as shadow


SESSION_DATE = "2026-09-01"
CANDIDATE_AT = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)
RESPONSE_AT = datetime(2026, 9, 1, 18, 0, 30, tzinfo=timezone.utc)
QUOTE_AT = "2026-09-01T18:00:15Z"


def candidate(*, grade="HIGH_PIN_PRESSURE", put_wall=595.0, call_wall=605.0):
    row = {
        "schema_version": "flashalpha-pin-candidate-v0",
        "record_type": "PIN_CANDIDATE",
        "session_date": SESSION_DATE,
        "symbol": "SPY",
        "observed_at_utc": CANDIDATE_AT.isoformat(),
        "grade": grade,
        "evidence": {
            "pocket": {"put_wall": put_wall, "call_wall": call_wall},
            "time_to_close_hours": 2.0,
        },
        "execution_authority": False,
    }
    row["record_hash"] = shadow.canonical_hash(row)
    return row


def option(strike, right, bid, ask, *, updated=QUOTE_AT):
    return {
        "underlying": "SPY", "expiry": SESSION_DATE, "strike": strike,
        "type": right, "bid": bid, "ask": ask, "lastUpdate": updated,
    }


def chain_rows(*, omit=None, stale=None, crossed=None):
    values = {
        "long_put": option(593, "P", 0.30, 0.40),
        "short_put": option(595, "P", 1.00, 1.10),
        "short_call": option(605, "C", 1.20, 1.30),
        "long_call": option(607, "C", 0.40, 0.50),
    }
    if omit:
        values.pop(omit)
    if stale:
        values[stale]["lastUpdate"] = "2026-09-01T17:50:00Z"
    if crossed:
        values[crossed]["bid"], values[crossed]["ask"] = 1.2, 1.1
    return list(values.values())


def chain_record(rows=None, *, status="AVAILABLE"):
    row = {
        "schema_version": ic.QUOTE_SCHEMA_VERSION,
        "record_type": "IC_OPTION_CHAIN_SNAPSHOT",
        "vendor": "FlashAlpha",
        "status": status,
        "response_at_utc": RESPONSE_AT.isoformat(),
        "returned_values": chain_rows() if rows is None else rows,
        "execution_authority": False,
    }
    return ic._finalize(row)


def close_record(close):
    return ic._finalize({
        "schema_version": ic.QUOTE_SCHEMA_VERSION,
        "record_type": "IC_UNDERLYING_CLOSE_SNAPSHOT",
        "vendor": "Alpaca", "feed": "SIP", "session_date": SESSION_DATE,
        "status": "AVAILABLE",
        "selected_bar": {"timestamp_utc": "2026-09-01T19:59:00+00:00", "close": close},
    })


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


class StaticChainClient:
    def __init__(self, record):
        self.record = record

    def fetch(self, **_kwargs):
        return self.record


class StaticCloseClient:
    def __init__(self, record):
        self.record = record

    def fetch(self, **_kwargs):
        return self.record


class Response:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def json(self):
        return self.payload


class Session:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_preregistration_hash_is_frozen_before_pnl_code():
    body = ic.PREREG_PATH.read_bytes()
    assert hashlib.sha256(body).hexdigest() == ic.PREREG_SHA256
    spec = ic._preregistration()
    assert spec["construction"]["wing_width_points"] == 2.0
    assert spec["entry"]["clock_et"] == "14:00:00"
    assert spec["pricing"]["total_entry_credit_haircut_points"] == 0.2
    assert spec["authority"]["execution_authority"] is False


def test_entry_uses_walls_exact_wings_real_mids_and_frozen_costs():
    entry = ic.build_entry(candidate(), chain_record())
    assert entry["status"] == "AVAILABLE"
    assert entry["cohort"] == "HIGH"
    assert entry["strikes"] == {
        "long_put": 593.0, "short_put": 595.0,
        "short_call": 605.0, "long_call": 607.0,
    }
    assert entry["gross_mid_credit_points"] == pytest.approx(1.50)
    assert entry["adjusted_credit_points"] == pytest.approx(1.30)
    assert entry["commission_dollars"] == 2.60
    assert entry["execution_authority"] is False


def test_nearest_short_strike_ties_are_conservative_and_deterministic():
    rows = chain_rows() + [
        option(594, "P", 0.5, 0.6), option(592, "P", 0.2, 0.3),
        option(606, "C", 0.8, 0.9), option(608, "C", 0.2, 0.3),
    ]
    entry = ic.build_entry(
        candidate(put_wall=594.5, call_wall=605.5), chain_record(rows),
    )
    assert entry["strikes"]["short_put"] == 595.0
    assert entry["strikes"]["short_call"] == 605.0


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        (chain_rows(omit="long_call"), "EXACT_TWO_POINT_WINGS_UNAVAILABLE"),
        (chain_rows(stale="short_call"), "SHORT_CALL_QUOTE_STALE"),
        (chain_rows(crossed="short_put"), "SHORT_PUT_QUOTE_ZERO_OR_CROSSED"),
    ],
)
def test_missing_or_bad_prices_remain_unavailable(rows, reason):
    entry = ic.build_entry(candidate(), chain_record(rows))
    assert entry["status"] == "UNAVAILABLE"
    assert entry["unavailable_reason"] == reason
    assert entry["adjusted_credit_points"] is None


def test_later_fresh_chain_cannot_backfill_fixed_entry():
    record = chain_record()
    record["response_at_utc"] = "2026-09-01T19:00:30+00:00"
    record["returned_values"] = chain_rows()
    for row in record["returned_values"]:
        row["lastUpdate"] = "2026-09-01T19:00:15Z"
    record = ic._finalize({key: value for key, value in record.items() if key != "record_hash"})
    entry = ic.build_entry(candidate(), record)
    assert entry["status"] == "UNAVAILABLE"
    assert entry["unavailable_reason"] == "OPTION_CHAIN_OUTSIDE_FIXED_ENTRY_WINDOW"


@pytest.mark.parametrize(
    ("close", "expected"),
    [(600.0, 127.40), (594.0, 27.40), (590.0, -72.60), (610.0, -72.60)],
)
def test_expiry_payoff_includes_partial_loss_caps_tail_and_costs(close, expected):
    entry = ic.build_entry(candidate(), chain_record())
    pnl = ic.settlement_pnl(entry, close)
    assert pnl["net_pnl_dollars"] == pytest.approx(expected)


def test_full_capture_settle_report_fixture_path(tmp_path):
    pin_path = tmp_path / "pin.jsonl"
    quote_path = tmp_path / "quotes.jsonl"
    study_path = tmp_path / "ic.jsonl"
    shadow.append_jsonl(pin_path, candidate())
    capture = ic.capture_entry(
        session_date=SESSION_DATE, pin_study_path=pin_path,
        quote_path=quote_path, ic_study_path=study_path,
        key_loader=lambda: "fixture-key",
        client_factory=lambda _key: StaticChainClient(chain_record()),
    )
    assert capture["status"] == "AVAILABLE"
    settlement = ic.settle_session(
        session_date=SESSION_DATE, quote_path=quote_path, ic_study_path=study_path,
        credential_loader=lambda: ("fixture-key", "fixture-secret"),
        client_factory=lambda _key, _secret: StaticCloseClient(close_record(600.0)),
    )
    assert settlement["status"] == "AVAILABLE"
    report = ic.build_report(ic_study_path=study_path, permutations=99)
    assert report["available_days"] == 1
    assert report["per_session"][0]["entry_credit_points"] == pytest.approx(1.5)
    assert report["per_session"][0]["spy_close"] == 600.0
    assert report["per_session"][0]["net_pnl_dollars"] == pytest.approx(127.4)
    assert report["session_block_matched_null"]["available"] is False
    assert len(read_rows(quote_path)) == 2
    assert [row["record_type"] for row in read_rows(study_path)] == ["IC_ENTRY", "IC_SETTLEMENT"]


def test_option_chain_rate_limit_is_logged_unavailable(tmp_path):
    pin_path = tmp_path / "pin.jsonl"
    quote_path = tmp_path / "quotes.jsonl"
    study_path = tmp_path / "ic.jsonl"
    shadow.append_jsonl(pin_path, candidate())
    result = ic.capture_entry(
        session_date=SESSION_DATE, pin_study_path=pin_path,
        quote_path=quote_path, ic_study_path=study_path,
        key_loader=lambda: "fixture-key",
        client_factory=lambda _key: StaticChainClient(chain_record([], status="RATE_LIMITED")),
    )
    assert result["status"] == "UNAVAILABLE"
    assert result["rate_limited"] is True
    assert result["entry"]["unavailable_reason"] == "OPTION_CHAIN_RATE_LIMITED"


def settlement_row(session_date, cohort, pnl):
    return ic._finalize({
        "schema_version": ic.SETTLEMENT_SCHEMA_VERSION,
        "record_type": "IC_SETTLEMENT", "session_date": session_date,
        "status": "AVAILABLE", "cohort": cohort,
        "pnl": {"net_pnl_dollars": pnl}, "win": pnl > 0,
    })


def test_report_compares_high_to_control_and_exposes_failed_pin_tail(tmp_path):
    path = tmp_path / "study.jsonl"
    rows = [
        settlement_row("2026-09-01", "HIGH", 127.4),
        settlement_row("2026-09-02", "HIGH", -72.6),
        settlement_row("2026-09-03", "NON_HIGH_CONTROL", 80.0),
        settlement_row("2026-09-04", "NON_HIGH_CONTROL", -120.0),
    ]
    for row in rows:
        shadow.append_jsonl(path, row)
    report_a = ic.build_report(ic_study_path=path, permutations=199)
    report_b = ic.build_report(ic_study_path=path, permutations=199)
    high = report_a["high_candidate_days"]
    assert high["mean_net_pnl_dollars"] == pytest.approx(27.4)
    assert high["win_rate"] == 0.5
    assert high["minimum_net_pnl_dollars"] == -72.6
    assert high["bottom_decile_mean_net_pnl_dollars"] == -72.6
    assert report_a["non_high_control_days"]["mean_net_pnl_dollars"] == -20.0
    assert report_a["high_minus_non_high_mean_net_pnl_dollars"] == pytest.approx(47.4)
    assert report_a["session_block_matched_null"] == report_b["session_block_matched_null"]
    assert report_a["is_inferential"] is False
    assert report_a["execution_authority"] is False


def test_flashalpha_live_option_chain_request_is_expiry_filtered():
    session = Session(Response(200, chain_rows(), {"X-RateLimit-Remaining": "2400"}))
    client = ic.FlashAlphaOptionChainClient(
        "fixture-key", session=session, clock=lambda: RESPONSE_AT,
    )
    record = client.fetch(expiry=SESSION_DATE, candidate_hash="candidate-hash")
    assert record["status"] == "AVAILABLE"
    url, kwargs = session.calls[0]
    assert url == "https://lab.flashalpha.com/optionquote/SPY"
    assert kwargs["params"] == {"expiry": SESSION_DATE}
    assert session.headers["X-Api-Key"] == "fixture-key"
    assert record["rate_limit"]["remaining"] == "2400"


def test_alpaca_close_uses_only_exact_1559_et_sip_bar():
    payload = {"bars": [
        {"t": "2026-09-01T19:58:00Z", "c": 599.8},
        {"t": "2026-09-01T19:59:00Z", "c": 600.2},
    ]}
    session = Session(Response(200, payload))
    client = ic.AlpacaCloseClient(
        "fixture-key", "fixture-secret", session=session,
        clock=lambda: datetime(2026, 9, 1, 20, 1, tzinfo=timezone.utc),
    )
    record = client.fetch(session_date=SESSION_DATE)
    assert record["status"] == "AVAILABLE"
    assert record["selected_bar"]["close"] == 600.2
    _, kwargs = session.calls[0]
    assert kwargs["params"]["feed"] == "sip"


def test_no_execution_collector_server_or_a2_imports():
    source = Path(ic.__file__).read_text()
    tree = ast.parse(source)
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    forbidden = {
        "scalp_server", "entry_bid_collector_v1", "entry_policy", "a2_measurement",
        "alpaca", "quant_core_shadow_feed",
    }
    assert imports.isdisjoint(forbidden)
    assert "submit_order" not in source
    assert "execution_authority\": True" not in source
