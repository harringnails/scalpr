import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import signal_threshold_calibration_v0 as calibration


def write_tick_fixture(path):
    fields = ("utc_time", "provider_ts", "symbol", "bid", "ask")
    start = datetime(2026, 8, 20, 13, 30, tzinfo=timezone.utc)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for session_offset in range(8):
            session = start + timedelta(days=session_offset)
            for second in range(0, 390 * 60, 5):
                ts = session + timedelta(seconds=second)
                mid = 100 + second * .00001
                if 600 <= second < 630:
                    mid -= .30
                row = {"utc_time": ts.isoformat(), "provider_ts": ts.isoformat(), "symbol": "SPY", "bid": mid - .01, "ask": mid + .01}
                writer.writerow(row)
        future = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)
        writer.writerow({"utc_time": future.isoformat(), "provider_ts": future.isoformat(), "symbol": "SPY", "bid": 999, "ask": 1000})


def write_databento_fixture(path, symbol="SPY"):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("ts_event", "symbol", "low", "close"))
        writer.writeheader()
        start = datetime(2026, 8, 20, 13, 30, tzinfo=timezone.utc)
        for minute in range(20):
            price = 100 if minute != 6 else 99.5
            writer.writerow({"ts_event": (start + timedelta(minutes=minute)).isoformat(), "symbol": symbol, "low": price, "close": 100})


def test_cutoff_excludes_future_rows_and_inputs_are_read_only(tmp_path):
    tick = tmp_path / "ticks.csv"
    write_tick_fixture(tick)
    before = tick.read_bytes()
    result = calibration.calibrate(tick_path=tick, completed_through=date(2026, 8, 27))
    assert "2026-09-04" not in result["sessions"]
    assert tick.read_bytes() == before
    assert result["included_rows"] > 0


def test_report_contains_every_proposed_and_default_value_and_fallbacks(tmp_path):
    tick = tmp_path / "ticks.csv"
    write_tick_fixture(tick)
    result = calibration.calibrate(tick_path=tick, completed_through=date(2026, 8, 27))
    report = calibration.render_report(result)
    for knob in "A W G S R V".split():
        assert f"`{knob}`" in report
    assert "Reasoned default" in report
    assert "FALLBACK" in report
    assert "NOT FROZEN" in report
    assert "N=150" in report and "p<=0.01" in report


def test_databento_spy_basis_is_labeled_noninferential_without_rescale(tmp_path):
    tick = tmp_path / "ticks.csv"
    bars = tmp_path / "bars.csv"
    write_tick_fixture(tick)
    write_databento_fixture(bars, symbol="SPY")
    result = calibration.calibrate(
        tick_path=tick, completed_through=date(2026, 8, 27), databento_path=bars,
    )
    report = calibration.render_report(result)
    assert result["databento"]["scale_to_spy"] == 1.0
    assert "no rescaling needed" in report
    assert "non-inferential distribution shape" in report


def test_non_spy_databento_points_are_never_proposed_unscaled(tmp_path):
    tick = tmp_path / "ticks.csv"
    bars = tmp_path / "bars.csv"
    write_tick_fixture(tick)
    write_databento_fixture(bars, symbol="SPX")
    result = calibration.calibrate(
        tick_path=tick, completed_through=date(2026, 8, 27), databento_path=bars,
    )
    assert result["databento"]["scale_to_spy"] is None
    assert "cannot supply point thresholds" in calibration.render_report(result)


def test_calibration_does_not_reference_or_modify_preregistrations():
    source = Path(calibration.__file__).read_text()
    assert "PREREG_prior_regime" not in source
    assert "PREREG_intraday" not in source
    assert "scalp_server" not in source
