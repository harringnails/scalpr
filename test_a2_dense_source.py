"""Network-free acceptance tests for the pre-registered A2 dense source."""

from datetime import datetime, timedelta, timezone
import json

import a2_dense_source as dense
import a2_measurement as a2


BASE = datetime(2026, 8, 14, 15, 55, tzinfo=timezone.utc)


def _episode(key="dense-episode"):
    return {
        "episode_key": key,
        "episode_record_id": f"record-{key}",
        "decision_id": f"decision-{key}",
        "cohort_id": "low_reversal_v1",
        "config_version": "entry-intelligence-config-v1.2.0",
        "config_hash": "frozen-config-hash",
        "symbol": "SPY",
        "side": "CALL",
        "session_date": "2026-08-14",
        "decided_at": BASE.isoformat(),
        "admitted": True,
    }


def _quote(at, mid, source=a2.DENSE_ENDPOINT_SOURCE):
    return {
        "provider_ts": at,
        "received_at": None,
        "mid": mid,
        "bid": mid - 0.01,
        "ask": mid + 0.01,
        "source": source,
        "endpoint_source": source,
    }


def _complete(source=a2.DENSE_ENDPOINT_SOURCE):
    rows = [_quote(BASE - timedelta(seconds=1), 100.0, source)]
    rows.extend(
        _quote(BASE + timedelta(minutes=horizon, seconds=-1), 100.0 + horizon / 10, source)
        for horizon in a2.HORIZONS_MIN
    )
    return rows


def test_dense_availability_where_live_tick_log_was_unavailable():
    legacy = _complete(a2.LEGACY_ENDPOINT_SOURCE)
    legacy = [row for row in legacy if row["provider_ts"] != BASE + timedelta(minutes=15, seconds=-1)]
    old_label = a2.label_episode(
        _episode(), session_quotes=legacy,
        endpoint_source=a2.LEGACY_ENDPOINT_SOURCE)
    new_label = a2.label_episode(
        _episode(), session_quotes=_complete(),
        endpoint_source=a2.DENSE_ENDPOINT_SOURCE)

    assert old_label["a2_outcome_status"] == "A2-UNAVAILABLE"
    assert new_label["a2_outcome_status"] == "A2-AVAILABLE"
    assert new_label["endpoint_source"] == "alpaca_historical_stock_quote_v1"


def test_dense_points_never_use_quotes_after_boundaries():
    quotes = [_quote(BASE - timedelta(seconds=1), 100.0)]
    expected = {}
    for horizon in a2.HORIZONS_MIN:
        boundary = BASE + timedelta(minutes=horizon)
        before = boundary - timedelta(milliseconds=250)
        quotes.extend([_quote(before, 100 + horizon), _quote(boundary + timedelta(milliseconds=1), 999)])
        expected[f"{horizon}m"] = before.isoformat()
    quotes.sort(key=lambda row: row["provider_ts"])

    label = a2.label_episode(
        _episode(), session_quotes=quotes,
        endpoint_source=a2.DENSE_ENDPOINT_SOURCE)

    assert label["label_status"] == "AVAILABLE"
    assert label["endpoint_provider_ts"] == expected
    assert all(age == 0.25 for age in label["endpoint_age_seconds"].values())


def test_dense_missing_or_crossed_quote_stays_unavailable_without_imputation():
    quotes = [_quote(BASE - timedelta(seconds=1), 100.0)]
    for horizon in a2.HORIZONS_MIN:
        boundary = BASE + timedelta(minutes=horizon)
        if horizon == 15:
            quotes.append(_quote(boundary - timedelta(seconds=5, milliseconds=1), 101.0))
            assert dense._source_quote({
                "t": (boundary - timedelta(seconds=1)).isoformat(),
                "bp": 102.0,
                "ap": 101.0,
            }) is None
        else:
            quotes.append(_quote(boundary - timedelta(seconds=1), 100 + horizon))

    label = a2.label_episode(
        _episode(), session_quotes=quotes,
        endpoint_source=a2.DENSE_ENDPOINT_SOURCE)

    assert label["label_status"] == "UNAVAILABLE"
    assert label["return_15m"] is None
    assert "missing_endpoint_15m_within_5s" in label["missing_reason"]
    assert label["endpoint_sources"]["15m"] == a2.DENSE_ENDPOINT_SOURCE
    assert label["endpoint_provider_ts"]["15m"] is None
    assert label["endpoint_age_seconds"]["15m"] is None


def test_provenance_distinguishes_legacy_and_dense_on_every_point():
    legacy = a2.label_episode(
        _episode("legacy"), session_quotes=_complete(a2.LEGACY_ENDPOINT_SOURCE),
        endpoint_source=a2.LEGACY_ENDPOINT_SOURCE)
    relabeled = a2.label_episode(
        _episode("dense"), session_quotes=_complete(),
        endpoint_source=a2.DENSE_ENDPOINT_SOURCE)

    assert legacy["endpoint_source"] == "live_tick_log"
    assert relabeled["endpoint_source"] == "alpaca_historical_stock_quote_v1"
    assert legacy["measurement_config_hash"] != relabeled["measurement_config_hash"]
    assert relabeled["anchor_source"] == a2.DENSE_ENDPOINT_SOURCE
    assert set(relabeled["endpoint_sources"].values()) == {a2.DENSE_ENDPOINT_SOURCE}
    assert set(relabeled["endpoint_age_seconds"]) == {"5m", "15m", "30m", "60m"}


def _activity_rows(gap_values, clean_values):
    rows = []
    for is_gap, values in ((True, gap_values), (False, clean_values)):
        rows.extend({
            "is_gap_minute": is_gap,
            "abs_return_1m": value,
            "quote_trade_intensity": value * 100,
            "atr_5m": value * 10,
        } for value in values)
    return rows


def test_bias_check_reproduces_known_synthetic_correlation_and_null():
    correlated = dense.characterize_gap_bias(
        _activity_rows(range(100, 120), range(40)))
    null = dense.characterize_gap_bias(
        _activity_rows(range(40), range(40)))

    assert correlated["verdict"] == "GAPS_CONDITION_CORRELATED"
    assert set(correlated["correlated_metrics"]) == {
        "abs_return_1m", "quote_trade_intensity", "atr_5m"}
    assert null["verdict"] == "GAPS_RANDOM"
    assert null["correlated_metrics"] == []


class _Response:
    status_code = 200
    text = ""

    def json(self):
        return {"quotes": {"SPY": [{
            "t": "2026-08-14T15:54:59Z", "bp": 100.0, "ap": 100.02,
        }]}, "next_page_token": None}


class _Session:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        return _Response()


def test_historical_client_is_sip_read_only_and_bounded():
    session = _Session()
    client = dense.AlpacaHistoricalStockDataClient(
        "key", "secret", session=session,
        base_url="https://example.invalid",
        request_timeout_seconds=2.5,
        fetch_deadline_seconds=5,
        max_pages=2)

    quotes = client.historical_quotes(
        "SPY", start=BASE - timedelta(seconds=5), end=BASE)

    assert quotes[0]["endpoint_source"] == a2.DENSE_ENDPOINT_SOURCE
    assert session.calls[0][1]["feed"] == "sip"
    assert session.calls[0][2] == 2.5
    assert "TradingClient" not in open(dense.__file__).read()


class _WindowClient:
    def historical_quotes(self, symbol, *, start, end):
        boundary = end - timedelta(microseconds=1)
        return [_quote(boundary - timedelta(seconds=1), 101.0)]


def test_uniform_relabel_and_verifier_use_only_genuine_fresh_dense_quotes(tmp_path):
    episodes = tmp_path / "episodes.jsonl"
    tick_log = tmp_path / "tick_log.csv"
    quarantine = tmp_path / "quarantine.jsonl"
    labels = tmp_path / "labels.jsonl"
    summary = tmp_path / "summary.json"
    comparison = tmp_path / "comparison.json"
    episodes.write_text(json.dumps(_episode()) + "\n")
    tick_log.write_text("utc_time,provider_ts,symbol,bid,ask\n")

    result = dense.run_uniform_relabel(
        _WindowClient(), episodes_path=episodes, tick_log_path=tick_log,
        quarantine_path=quarantine, output_path=labels,
        summary_path=summary, comparison_path=comparison)
    verification = dense.verify_uniform_relabel(
        labels_path=labels, comparison_path=comparison)

    assert result["endpoint_source"] == a2.DENSE_ENDPOINT_SOURCE
    assert result["source_comparison"]["availability_rise_count"] == 1
    assert verification["status"] == "PASS"


def test_dense_source_is_not_imported_by_server_startup_path():
    server_source = (dense.Path(dense.__file__).with_name("scalp_server.py")).read_text()
    assert "a2_dense_source" not in server_source
