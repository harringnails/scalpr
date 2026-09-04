import hashlib
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import multi_instrument_signal_v0 as study


SESSION = "2026-09-08"
OPEN = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload, self.status_code, self.headers = payload, status, headers or {}
    def json(self): return self.payload
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)


def quote(stamp, mid, symbol="SPY"):
    return {"record_type": "MULTI_INSTRUMENT_SIP_QUOTE", "symbol": symbol,
            "provider_ts": stamp.isoformat(), "received_at_utc": (stamp + timedelta(milliseconds=10)).isoformat(),
            "bid": mid - .01, "ask": mid + .01, "mid": mid, "clean": True,
            "age_seconds": .01, "source": "Alpaca:SIP:fixture"}


def structure(stamp, symbol="SPY", **values):
    base = {"gamma_regime": "negative", "gamma_flip": 100, "spot": 99,
            "call_wall": 101, "put_wall": 98}
    base.update(values)
    return {"record_type": "MULTI_INSTRUMENT_STRUCTURE", "symbol": symbol,
            "observed_at_utc": stamp.isoformat(), "values": base,
            "freshness": {"status": "FRESH", "age_seconds": 1},
            "endpoint_provenance": {name: {"provider_ts": stamp.isoformat(), "status": "AVAILABLE"}
                                    for name in study.ENDPOINTS}, "record_hash": f"{symbol}-{stamp}"}


def prereg(tmp_path):
    path = tmp_path / "prereg.md"; path.write_text("FROZEN fixture")
    return path


def test_rate_gate_passes_before_poll_and_has_headroom(tmp_path):
    assert study.rate_budget() == {"fits": True, "limit": 2500, "multi_calls": 948,
                                   "existing_calls": 238, "total_calls": 1186, "headroom": 1314}
    original = study.FLASH_DAILY_LIMIT
    try:
        study.FLASH_DAILY_LIMIT = 900
        try: study.flash_poll_once(output=tmp_path / "x", api_key="secret", requester=lambda *a, **k: None)
        except RuntimeError as exc: assert "budget gate" in str(exc)
        else: raise AssertionError("poll bypassed rate gate")
    finally: study.FLASH_DAILY_LIMIT = original


def test_flash_poll_all_symbols_and_provenance(tmp_path):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        payload = {"as_of": "2026-09-08T14:00:00Z", "spot": 100, "net_gex": -1,
                   "gamma_flip": 101, "call_wall": 102, "put_wall": 98}
        return Response(payload, headers={"X-RateLimit-Limit": "2500", "X-RateLimit-Remaining": "2400"})
    output = tmp_path / "flash.jsonl"
    result = study.flash_poll_once(output=output, api_key="secret", requester=get,
                                   now=lambda: datetime(2026, 9, 8, 14, 0, 1, tzinfo=timezone.utc))
    rows = study.read_jsonl(output)
    assert result == {"attempted": 12, "rate_limited": False}
    assert len(calls) == 12 and {row["symbol"] for row in rows} == set(study.SYMBOLS)
    assert all(row["freshness"]["status"] == "FRESH" for row in rows)
    assert all(row["execution_authority"] is False for row in rows)


def test_alpaca_batch_marks_crossed_one_symbol_only(tmp_path):
    payload = {"quotes": {symbol: {"bp": 99, "ap": 100, "t": "2026-09-08T14:00:00Z"}
                          for symbol in study.SYMBOLS}}
    payload["quotes"]["IWM"] = {"bp": 101, "ap": 100, "t": "2026-09-08T14:00:00Z"}
    output = tmp_path / "market.jsonl"
    study.alpaca_poll_once(output=output, api_key="k", api_secret="s", requester=lambda *a, **k: Response(payload),
                           now=lambda: datetime(2026, 9, 8, 14, 0, 1, tzinfo=timezone.utc))
    states = {row["symbol"]: row["clean"] for row in study.read_jsonl(output)}
    assert states == {"SPY": True, "QQQ": True, "IWM": False, "DIA": True}


def test_bps_thresholds_are_price_invariant():
    assert study.bps_points(100, 2.5) == .025
    assert study.bps_points(1000, 2.5) == .25
    assert study.bps_points(100, 3.0) / 100 == study.bps_points(1000, 3.0) / 1000


def test_d1_routes_h1b_freezes_prior_flip_and_stamps_hash(tmp_path):
    rows = [quote(OPEN + timedelta(seconds=i), 99 if i == 0 else 100.05) for i in range(0, 4600, 5)]
    flashes = study.structures([structure(OPEN - timedelta(days=1), gamma_flip=100),
                                structure(OPEN + timedelta(minutes=5), gamma_flip=120)], "SPY")
    result = study.evaluate_d1(SESSION, "SPY", study.market_quotes(rows, "SPY", SESSION), flashes, prereg(tmp_path))
    assert result["cohort"] == "H1b" and result["frozen_flip"] == 100
    assert result["counts_toward_n"] is True
    assert result["frozen_prereg_sha256"] == hashlib.sha256(b"FROZEN fixture").hexdigest()


def test_d2_sequence_t0_is_migration_confirmation_and_volume_is_absent(tmp_path):
    rows = []
    for seconds in range(0, 121 * 60, 5):
        mid = 100.0 if seconds < 300 else 99.96 if seconds < 360 else 100.0 + (seconds - 360) * .001
        rows.append(quote(OPEN + timedelta(seconds=seconds), mid))
    migration = OPEN + timedelta(minutes=32)
    flashes = study.structures([structure(OPEN + timedelta(minutes=2), call_wall=101.0, gamma_regime="positive"),
                                structure(migration, call_wall=102.0, gamma_regime="positive")], "SPY")
    result = study.evaluate_d2(SESSION, "SPY", study.market_quotes(rows, "SPY", SESSION), flashes, prereg(tmp_path))
    assert result["counts_toward_n"] is True
    assert result["anchor_t0_utc"] == migration.isoformat()
    assert result["proxy_vwap_basis"] == "quote_mid_proxy_vwap_not_traded_vwap"
    assert "volume" not in inspect.getsource(study.evaluate_d2).lower()


def test_flashalpha_spot_is_aligned_to_same_instrument_sip_quote():
    stamp = OPEN + timedelta(minutes=5)
    aligned = study.align_structures(study.structures([structure(stamp, spot=100)], "SPY"),
                                     study.all_symbol_quotes([quote(stamp, 100.1)], "SPY"))
    assert aligned[0]["alpaca_alignment"]["status"] == "AVAILABLE"
    assert aligned[0]["alpaca_alignment"]["spot_delta_bps"] == 10.0


def test_prelock_and_stale_instrument_are_excluded_without_blocking(tmp_path):
    frozen = prereg(tmp_path)
    assert study.evaluate_d1("2026-09-04", "SPY", [], [], frozen)["exclusion_reason"] == "PRELOCK_IN_SAMPLE"
    fresh = study.structures([structure(OPEN - timedelta(days=1), "SPY")], "SPY")
    stale = study.structures([{**structure(OPEN - timedelta(days=1), "QQQ"), "freshness": {"status": "STALE_OR_UNAVAILABLE"}}], "QQQ")
    assert study.evaluate_d1(SESSION, "SPY", [], fresh, frozen)["exclusion_reason"] != "MISSING_PRIOR_CLOSE_STRUCTURE"
    assert study.evaluate_d1(SESSION, "QQQ", [], stale, frozen)["exclusion_reason"] == "MISSING_PRIOR_CLOSE_STRUCTURE"


def test_matched_null_is_instrument_stratified_and_reported():
    def episode(symbol, day, value, control_day, control):
        return {"arm": "D2", "cohort": "SINGLE", "instrument": symbol, "session_date": day,
                "counts_toward_n": True, "outcome": {"returns": {"return_60m": value}},
                "match_key": {"instrument": symbol, "session_time_block": "10:00", "realized_vol_bucket": "MID"},
                "control_pool": [{"instrument": symbol, "session_date": control_day,
                                  "session_time_block": "10:00", "realized_vol_bucket": "MID", "return_60m": control}]}
    rows = [episode("SPY", "2026-09-08", .02, "2026-09-09", .01),
            episode("QQQ", "2026-09-09", .03, "2026-09-08", .01)]
    report = study.summarize(rows)["groups"]["D2:SINGLE"]
    assert report["n"] == 2 and report["matched_n"] == 2
    assert report["mean_matched_effect_60m"] == .015
    assert report["verdict"] == "UNDERPOWERED"


def test_large_sample_permutation_is_deterministic_and_uses_all_effects():
    effects = [0.001 + index / 1_000_000 for index in range(30)]
    assert study.permutation_p(effects) == study.permutation_p(effects)
    changed = effects.copy(); changed[25] = -1
    assert study.permutation_p(effects) != study.permutation_p(changed)


def test_frozen_prereg_exact_and_isolation():
    text = Path("PREREG_multi_instrument_signal_v0.md").read_text()
    for token in ("2.5 bps", "3.0 bps", "180 seconds", "900 seconds", "120 seconds", "2 minutes", "N = 150", "p <= 0.01", "3/4"):
        assert token in text
    source = inspect.getsource(study).lower()
    for forbidden in ("scalp_server", "submittrade", "order_adapter", "guard_events", "admission_authority"):
        assert forbidden not in source


def test_spy_only_logger_sources_are_byte_unchanged():
    expected = {"prior_regime_flip_reclaim_logger_v0.py": "a0e8ccd76804354a7ab1c92a49f4cfb6720b263ead46c99cd82936cacc7cc8f5",
                "intraday_continuation_logger_v0.py": "0bc9fe93a24b402dc80c11aa8f41e2da514dddd2714f8316604d946e2020ed3c"}
    for name, digest in expected.items(): assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest
