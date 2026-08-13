"""Network-free tests for the read-only Alpaca native-index adapter."""

from datetime import datetime, timezone

from alpaca_index_data import AlpacaIndexDataClient, IndexDataError


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
        self.text = str(payload)

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        return self.responses.pop(0)


def client(responses):
    return AlpacaIndexDataClient(
        "test-key", "test-secret", session=Session(responses),
        base_url="https://example.invalid", refresh_seconds=60)


def test_latest_spx_value_preserves_provider_timestamp():
    c = client([Response({"values": {
        "SPX": {"t": "2026-08-05T15:00:02.0256Z", "v": 7734.46}}})])
    row = c.latest_value("spx")
    assert row["value"] == 7734.46
    assert row["provider_ts_iso"].startswith("2026-08-05T15:00:02.025600")
    assert c.session.calls[0][0].endswith("/v1beta1/indices/latest/values")
    assert c.session.calls[0][1]["symbols"] == "SPX"


def test_historical_pagination_and_minute_aggregation_are_bounded():
    page1 = {"values": {"SPX": [
        {"t": "2026-08-05T13:30:00Z", "v": 7700.0},
        {"t": "2026-08-05T13:30:30Z", "v": 7702.0},
    ]}, "next_page_token": "next"}
    page2 = {"values": {"SPX": [
        {"t": "2026-08-05T13:30:59Z", "v": 7699.0},
        {"t": "2026-08-05T13:31:01Z", "v": 7703.0},
    ]}, "next_page_token": None}
    c = client([Response(page1), Response(page2)])
    opened = datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc)
    bars = c.minute_bars(
        "SPX", session_open=opened,
        now=datetime(2026, 8, 5, 13, 32, tzinfo=timezone.utc))
    assert bars == [
        {"t": 0, "high": 7702.0, "low": 7699.0, "close": 7699.0,
         "volume": 3.0, "sample_count": 3},
        {"t": 1, "high": 7703.0, "low": 7703.0, "close": 7703.0,
         "volume": 1.0, "sample_count": 1},
    ]
    assert len(c.session.calls) == 2
    assert c.session.calls[1][1]["page_token"] == "next"
    # Cache hit: no third network request.
    assert c.minute_bars(
        "SPX", session_open=opened,
        now=datetime(2026, 8, 5, 13, 32, tzinfo=timezone.utc)) == bars
    assert len(c.session.calls) == 2


def test_entitlement_error_fails_closed_without_leaking_credentials():
    c = client([Response({"message": "subscription does not permit access"}, 403)])
    try:
        c.latest_value("SPX")
    except IndexDataError as exc:
        message = str(exc)
        assert "HTTP 403" in message and "test-key" not in message
    else:
        raise AssertionError("403 response was accepted")


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_"):
            test()
            print("PASS", name)

