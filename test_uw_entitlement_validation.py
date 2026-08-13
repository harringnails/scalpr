"""Network-free tests for sanitized UW entitlement validation and replay."""

from datetime import datetime, timedelta, timezone

from uw_entitlement_validation import build_controlled_replay, probe_endpoint


NOW = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)


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
        self.calls.append((url, params, timeout))
        return self.responses.pop(0)


def row(event_id="a"):
    observed = int((NOW - timedelta(minutes=1)).timestamp() * 1000)
    return {
        "id": event_id, "ticker": "SPY",
        "option_chain": "SPY260807C00775000", "executed_at": observed,
        "price": "1.40", "total_size": 10, "total_premium": 14000,
        "total_ask_side_prem": 12000, "total_bid_side_prem": 2000,
        "volume": 50, "open_interest": 100, "has_sweep": True,
    }


def test_probe_reports_entitlement_without_response_body_or_credentials():
    session = Session([Response({"data": [row()]})])
    result = probe_endpoint(session, "flow_alerts", "stock/SPY/flow-alerts", {"limit": 1})
    assert result["status"] == "AVAILABLE" and result["record_count"] == 1
    assert result["http_status"] == 200
    assert "authorization" not in str(result).lower()


def test_probe_classifies_forbidden_and_rate_limited():
    forbidden = probe_endpoint(Session([Response({}, 403)]), "x", "x", None)
    limited = probe_endpoint(Session([Response({}, 429)]), "x", "x", None)
    assert forbidden["status"] == "FORBIDDEN"
    assert limited["status"] == "RATE_LIMITED"
    assert forbidden["payload"] is None and limited["payload"] is None


def test_controlled_replay_is_deterministic_deduplicated_and_no_lookahead():
    report = build_controlled_replay([row(), row(), row("b")], received_at=NOW)
    assert report["input_rows"] == 3 and report["unique_events"] == 2
    assert report["duplicates_removed"] == 1
    assert report["deterministic"] is True
    assert report["lookahead_violations"] == 0
    assert report["fixed_sample_event_count"] == 2
    assert report["fixed_sample_snapshot"]["recommendation"] is None
    assert len(report["event_hash"]) == 64 and len(report["snapshot_hash"]) == 64


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_"):
            test()
            print("PASS", name)
