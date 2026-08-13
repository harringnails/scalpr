"""Network-free tests for the bounded provider capture schedule."""

from datetime import datetime
from zoneinfo import ZoneInfo

from scalp_server import _provider_capture_window

ET = ZoneInfo("America/New_York")


def check(name, condition):
    print(f"  {'ok' if condition else 'FAIL'}: {name}")
    assert condition, name


def test_flow_runs_only_in_session_and_ivol_after_close():
    pre = _provider_capture_window(datetime(2026, 8, 5, 9, 29, tzinfo=ET))
    regular = _provider_capture_window(datetime(2026, 8, 5, 12, 0, tzinfo=ET))
    after = _provider_capture_window(datetime(2026, 8, 5, 16, 20, tzinfo=ET))
    check("flow off before open", pre["flow_active"] is False)
    check("flow on during session", regular["flow_active"] is True)
    check("IVol not due intraday", regular["ivol_eod_due"] is False)
    check("flow off after session", after["flow_active"] is False)
    check("IVol due after close", after["ivol_eod_due"] is True)


def test_weekends_are_quiet():
    saturday = _provider_capture_window(datetime(2026, 8, 8, 17, 0, tzinfo=ET))
    check("no weekend flow", saturday["flow_active"] is False)
    check("no weekend EOD capture", saturday["ivol_eod_due"] is False)


if __name__ == "__main__":
    test_flow_runs_only_in_session_and_ivol_after_close()
    test_weekends_are_quiet()
    print("\nALL PROVIDER CAPTURE SCHEDULE TESTS PASSED")
