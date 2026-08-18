"""Network-free tests for the Scalpr V2 options-intelligence Phase 1 slice."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ivolatility_adapter import IVolatilityClient, IVolatilityError, IVolatilityUnavailable
from options_feature_store import OptionsFeatureStore
from options_capture_service import OptionsCaptureService, capture_result_disposition
from options_intelligence import (
    build_chain_snapshot, canonical_hash, engineer_options_features,
    normalize_ivolatility_row,
)


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        return self.responses.pop(0)


def row(right="C", strike=775.0, oi=100, volume=20, iv=.2, gamma=.01):
    return {
        "c_date": "2026-08-05", "option_symbol": f"SPY TEST {right} {strike}",
        "dte": 2, "expiration_date": "2026-08-07", "call_put": right,
        "price_strike": strike, "underlying_price": 773.0,
        "Bid": 2.0, "Ask": 2.2, "volume": volume, "openinterest": oi,
        "iv": iv, "delta": .5 if right == "C" else -.5,
        "gamma": gamma, "theta": -.1, "vega": .05,
    }


def test_missing_key_is_disabled_and_fails_before_network():
    session = Session([])
    client = IVolatilityClient("", session=session)
    assert client.status()["configured"] is False
    try:
        client.fetch_eod_chain(symbol="SPY", trade_date="2026-08-05")
    except IVolatilityUnavailable as exc:
        assert "IVOLATILITY_API_KEY" in str(exc)
    else:
        raise AssertionError("missing key was accepted")
    assert session.calls == []


def test_scope_is_spy_zero_to_two_dte_only():
    client = IVolatilityClient("secret", session=Session([]))
    for kwargs in ({"symbol": "QQQ", "dte_from": 0, "dte_to": 2},
                   {"symbol": "SPY", "dte_from": 0, "dte_to": 7}):
        try:
            client.fetch_eod_chain(trade_date="2026-08-05", **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError("out-of-scope request was accepted")


def test_adapter_fetches_both_rights_and_never_returns_key():
    session = Session([
        Response({"status": {"code": "OK"}, "data": [row("C")]}),
        Response({"status": {"code": "OK"}, "data": [row("P", 770)]}),
    ])
    client = IVolatilityClient("secret-value", session=session,
                               base_url="https://example.invalid")
    result = client.fetch_eod_chain(symbol="SPY", trade_date="2026-08-05")
    assert result["source_status"] == "ready"
    assert [call[1]["cp"] for call in session.calls] == ["C", "P"]
    assert "secret-value" not in json.dumps(result)
    assert "apiKey" not in result["query"]


def test_http_error_is_sanitized():
    client = IVolatilityClient(
        "secret-value", session=Session([Response({}, 403)]),
        base_url="https://example.invalid")
    try:
        client.fetch_eod_chain(symbol="SPY", trade_date="2026-08-05")
    except IVolatilityError as exc:
        assert str(exc) == "IVolatility API HTTP 403"
        assert "secret-value" not in str(exc)
    else:
        raise AssertionError("403 was accepted")


def test_normalization_features_and_dealer_positioning_fail_closed():
    contracts = [normalize_ivolatility_row(row("C"), underlying="SPY"),
                 normalize_ivolatility_row(row("P", 770, oi=150, volume=30, iv=.24),
                                           underlying="SPY")]
    raw_hash = canonical_hash({"data": [row("C"), row("P")]})
    snapshot = build_chain_snapshot(
        provider="ivolatility", underlying="SPY",
        captured_at="2026-08-05T21:00:00+00:00", contracts=contracts,
        raw_payload_hash=raw_hash)
    features = engineer_options_features(snapshot)
    assert features["put_call_open_interest_ratio"] == 1.5
    assert features["put_call_iv_skew"] > 0
    assert features["unsigned_gamma_concentration"] == 250.0
    assert features["dealer_gamma_exposure"] is None
    assert features["dealer_hedge_pressure"] is None
    assert features["recommendation"] is None and features["qualifying"] is False


def test_missing_fields_are_explicit():
    incomplete = row()
    del incomplete["gamma"]
    contract = normalize_ivolatility_row(incomplete, underlying="SPY")
    assert contract.gamma is None
    assert "gamma" in contract.missing_fields


def test_store_is_append_only_and_deduplicated():
    contract = normalize_ivolatility_row(row(), underlying="SPY")
    raw = {"provider": "ivolatility", "payloads": [{"data": [row()]}]}
    snapshot = build_chain_snapshot(
        provider="ivolatility", underlying="SPY",
        captured_at="2026-08-05T21:00:00+00:00", contracts=[contract],
        raw_payload_hash=canonical_hash(raw))
    features = engineer_options_features(snapshot)
    with TemporaryDirectory() as directory:
        store = OptionsFeatureStore(Path(directory))
        first = store.persist(raw_capture=raw, snapshot=snapshot, features=features)
        second = store.persist(raw_capture=raw, snapshot=snapshot, features=features)
        assert all(first["created"].values())
        assert not any(second["created"].values())
        assert store.status()["raw_captures"] == 1
        assert len(store.manifest_path.read_text().splitlines()) == 1


def test_capture_pipeline_promotes_only_complete_responses():
    session = Session([
        Response({"status": {"code": "OK"}, "data": [row("C")]}),
        Response({"status": {"code": "OK"}, "data": [row("P", 770)]}),
    ])
    with TemporaryDirectory() as directory:
        service = OptionsCaptureService(
            IVolatilityClient("secret", session=session,
                              base_url="https://example.invalid"),
            OptionsFeatureStore(Path(directory)),
            clock=lambda: "2026-08-05T21:00:00+00:00",
        )
        result = service.capture_eod(symbol="SPY", trade_date="2026-08-05")
        assert result["promoted"] is True
        assert result["contract_count"] == 2
        assert result["rejected_rows"] == []


def test_pending_capture_is_raw_only_and_not_promoted():
    session = Session([
        Response({"status": {"code": "PENDING"}, "data": []}),
        Response({"status": {"code": "PENDING"}, "data": []}),
    ])
    with TemporaryDirectory() as directory:
        store = OptionsFeatureStore(Path(directory))
        service = OptionsCaptureService(
            IVolatilityClient("secret", session=session,
                              base_url="https://example.invalid"), store)
        result = service.capture_eod(symbol="SPY", trade_date="2026-08-05")
        assert result["source_status"] == "pending"
        assert result["promoted"] is False
        status = store.status()
        assert status["raw_captures"] == 1
        assert status["snapshots"] == 0 and status["feature_records"] == 0


def test_empty_capture_is_not_complete_and_retries():
    disposition = capture_result_disposition({
        "source_status": "no_usable_contracts",
        "promoted": False,
        "contract_count": 0,
    })
    assert disposition == {
        "complete": False,
        "health_status": "DEGRADED",
        "retry_seconds": 900.0,
    }


def test_nonempty_promoted_capture_is_complete():
    disposition = capture_result_disposition({
        "source_status": "ready",
        "promoted": True,
        "contract_count": 2,
    })
    assert disposition == {
        "complete": True,
        "health_status": "OK",
        "retry_seconds": None,
    }


def test_runtime_status_exposes_empty_capture_retry_policy():
    source = Path("scalp_server.py").read_text(encoding="utf-8")
    assert '"completion_policy": "PROMOTED_NONEMPTY_ONLY"' in source
    assert '"empty_capture_retry_seconds": 900' in source


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_"):
            test()
            print("PASS", name)
