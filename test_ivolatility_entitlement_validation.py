"""Network-free tests for the sanitized IVolatility capability validator."""

from datetime import datetime, timezone

from ivolatility_entitlement_validation import probe, validate


class Response:
    def __init__(self, payload=None, status=200):
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


def chain_row(right="C"):
    return {
        "c_date": "2026-08-04", "option_symbol": f"SPY260805{right}00775000",
        "dte": 1, "expiration_date": "2026-08-05", "call_put": right,
        "price_strike": 775, "price": 1.4, "volume": 50,
        "openinterest": 100, "iv": .20, "delta": .5 if right == "C" else -.5,
        "gamma": .01, "theta": -.10, "vega": .05, "Ask": 1.45,
        "Bid": 1.35, "underlying_price": 773.2,
    }


def test_probe_distinguishes_pending_forbidden_and_no_data_without_key_output():
    pending = probe(
        Session([Response({"status": {"code": "PENDING"}, "data": []})]),
        name="x", path="/x", params={}, key="secret")
    forbidden = probe(
        Session([Response(status=403)]), name="x", path="/x", params={}, key="secret")
    no_data = probe(
        Session([Response(status=204)]), name="x", path="/x", params={}, key="secret")
    assert pending["status"] == "PENDING"
    assert forbidden["status"] == "FORBIDDEN"
    assert no_data["status"] == "NO_DATA"
    assert "secret" not in str(pending) + str(forbidden) + str(no_data)


def test_full_validator_normalizes_chain_and_builds_deterministic_features():
    # Six fixed probes plus three contract-dependent probes.
    session = Session([
        Response({"data": [chain_row("C")]}),
        Response({"data": [chain_row("P")]}),
        Response({"data": [{"symbol": "SPY", "ivr30": 50, "ivp30": 60}]}),
        Response({"data": [{"symbol": "SPY", "20d_hv": .15}]}),
        Response({"data": [{"symbol": "SPY", "moneyness": 0, "iv": .2}]}),
        Response({"data": [{"symbol": "SPY", "ivx": .2}]}),
        Response({"data": [{"date": "2026-08-04", "iv": .2}]}),
        Response({"data": [{"symbol": "SPY260805C00775000", "iv": .2}]}),
        Response(status=403),
    ])
    report, raw = validate(
        session, key="secret", trade_date="2026-08-04",
        now=datetime(2026, 8, 5, 20, tzinfo=timezone.utc), spacing_seconds=0)
    assert report["request_count"] == 9
    assert report["normalization"]["normalized_contracts"] == 2
    assert report["normalization"]["rejected_contracts"] == 0
    assert report["replay"]["deterministic"] is True
    assert report["replay"]["recommendation"] is None
    assert report["endpoints"][-1]["status"] == "FORBIDDEN"
    assert "secret" not in str(report) and "apiKey" not in str(report)
    assert raw["chain_calls"]["data"][0]["call_put"] == "C"


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_"):
            test()
            print("PASS", name)
