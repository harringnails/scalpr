"""Network-free tests for explicit entry-policy feed and NBBO attribution."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import entry_policy


def check(name, condition):
    print(f"  {'ok' if condition else 'FAIL'}: {name}")
    assert condition, name


class Quotes:
    def __init__(self, quote):
        self.quote = quote
        self.request = None
    def get_stock_latest_quote(self, request):
        self.request = request
        return {"SPY": self.quote}


def test_sip_nbbo_is_explicit_and_timestamp_audited():
    now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    client = Quotes(SimpleNamespace(
        timestamp=now - timedelta(seconds=1), bid_price=700.0, ask_price=700.02))
    result = entry_policy._nbbo_context(client, "SPY", "sip", now=now)
    check("fresh two-sided NBBO is available", result["status"] == "available")
    check("spread is measured", result["spread"] == 0.02 and result["spread_bps"] > 0)
    check("SIP requested explicitly", str(client.request.feed.value).lower() == "sip")


def test_stale_nbbo_is_not_treated_as_tradable():
    now = datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc)
    client = Quotes(SimpleNamespace(
        timestamp=now - timedelta(minutes=2), bid_price=700.0, ask_price=700.02))
    result = entry_policy._nbbo_context(client, "SPY", "sip", now=now)
    check("stale quote is explicit", result["status"] == "stale")
    check("stale age preserved", result["age_seconds"] == 120.0)


def test_closed_market_assessment_abstains_without_a_data_client():
    result = entry_policy.market_closed_assessment("spy", feed="sip")
    check("closed market is a hard abstention", result["decision"] == "NO_TRADE")
    check("no current price is fabricated", result["price"] is None)
    check("closed SIP status explicit", result["feed_status"] == "sip_market_closed")
    check("formal policy remains blocked", result["formal_readiness"] == "NO_TRADE")


if __name__ == "__main__":
    test_sip_nbbo_is_explicit_and_timestamp_audited()
    test_stale_nbbo_is_not_treated_as_tradable()
    test_closed_market_assessment_abstains_without_a_data_client()
    print("\nALL ENTRY FEED CONTEXT TESTS PASSED")
