import json
from datetime import datetime, timedelta, timezone

import market_context_shadow_v0 as context


NOW = datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc)


def bar(minutes, price, volume=100):
    return {
        "close": price, "high": price + 0.1, "low": price - 0.1, "open": price - 0.05,
        "timestamp": (NOW - timedelta(minutes=minutes)).isoformat(), "volume": volume,
    }


def raw_fixture():
    bars = {}
    quotes = {}
    for index, symbol in enumerate(context.ALL_SYMBOLS):
        price = 100.0 + index
        quotes[symbol] = {
            "bid": price - 0.01, "ask": price + 0.01,
            "timestamp": (NOW - timedelta(seconds=2)).isoformat(),
        }
        bars[symbol] = [bar(2, price - 0.2), bar(1, price)]
    bars["SPY"] = [bar(32 - index, 100 + index * 0.02) for index in range(32)]
    return {"bars": bars, "quotes": quotes}


def flash_fixture(ts=None):
    timestamp = ts or (NOW - timedelta(seconds=5)).isoformat()
    return {
        "record_type": "PIN_CANDIDATE", "symbol": "SPY", "observed_at_utc": timestamp,
        "evidence": {
            "call_wall": {"price": 102}, "gamma_flip": 99, "gamma_regime": "positive",
            "put_wall": {"price": 98}, "spot": 100, "spot_to_call_wall_bps": 200,
            "spot_to_put_wall_bps": 200, "zero_dte_gex_share_pct": 45,
            "data_freshness": {"as_of_by_endpoint": {"zero_dte": timestamp}},
        },
    }


def envelopes(value):
    if isinstance(value, dict):
        if "value" in value and "data_freshness" in value:
            yield value
        else:
            for child in value.values():
                yield from envelopes(child)


def test_every_context_field_is_freshness_stamped_and_proxy_is_honest():
    record = context.build_record(raw_fixture(), observed_at=NOW, flashalpha=flash_fixture())
    all_envelopes = list(envelopes(record["fields"]))
    assert all_envelopes
    assert all("provider_timestamp_utc" in item and "age_seconds" in item for item in all_envelopes)
    breadth = record["fields"]["largecap_breadth"]
    assert "largecap_breadth_proxy" in breadth
    assert "sp500_breadth" not in json.dumps(record).lower()
    assert breadth["largecap_breadth_proxy"]["classification"] == "proxy"
    assert record["execution_authority"] is False and record["guard_access"] is False
    assert record["sub_scores"] == "DEFERRED_UNTIL_OPERATOR_LOCK"


def test_stale_missing_and_future_values_are_not_imputed():
    raw = raw_fixture()
    raw["quotes"]["SPY"] = {"bid": None, "ask": None, "timestamp": None}
    future = flash_fixture((NOW + timedelta(minutes=5)).isoformat())
    record = context.build_record(raw, observed_at=NOW, flashalpha=future)
    spy_mid = record["fields"]["spy_structure"]["spy_mid"]
    assert spy_mid["value"] is None and spy_mid["status"] == "MISSING"
    gamma = record["fields"]["options_structure"]["gamma_regime"]
    assert gamma["value"] is None and gamma["status"] == "FUTURE_REJECTED"
    assert record["data_freshness"] == "STALE"


def test_future_bars_are_excluded_from_computation():
    raw = raw_fixture()
    raw["bars"]["SPY"].append({
        "close": 9999, "high": 9999, "low": 9999, "open": 9999, "volume": 1_000_000,
        "timestamp": (NOW + timedelta(minutes=1)).isoformat(),
    })
    with_future = context.build_record(raw, observed_at=NOW, flashalpha=flash_fixture())
    raw["bars"]["SPY"].pop()
    without_future = context.build_record(raw, observed_at=NOW, flashalpha=flash_fixture())
    assert with_future["fields"]["spy_structure"]["vwap_proxy"] == without_future["fields"]["spy_structure"]["vwap_proxy"]


def test_capture_writes_only_requested_ledger(tmp_path):
    class Source:
        def fetch(self, now):
            return raw_fixture()

    output = tmp_path / "isolated" / "market_context.jsonl"
    record = context.capture_once(Source(), output=output, flashalpha_ledger=None, now=NOW)
    assert output.exists()
    assert len(list(tmp_path.rglob("*.*"))) == 1
    assert json.loads(output.read_text())["record_hash"] == record["record_hash"]
