import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import signal_study_session_runner_v0 as runner


OPEN = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
CLOSE = datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc)
CALENDAR = {"date": "2026-09-08", "open": "09:30", "close": "16:00"}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Child:
    def __init__(self, code=0):
        self.code = code

    def wait(self):
        return self.code


class Completed:
    def __init__(self, code=0):
        self.returncode = code


def test_calendar_closed_day_and_session_bounds():
    closed = runner.market_calendar(
        date(2026, 9, 7), api_key="key", api_secret="secret",
        requester=lambda *_args, **_kwargs: Response([]),
    )
    assert closed is None
    row = runner.market_calendar(
        date(2026, 9, 8), api_key="key", api_secret="secret",
        requester=lambda *_args, **_kwargs: Response([CALENDAR]),
    )
    assert runner.session_bounds(row) == (OPEN, CLOSE)


def test_early_close_shortens_both_poll_streams():
    early_close = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    now = datetime(2026, 11, 27, 14, 30, 1, tzinfo=timezone.utc)
    commands = runner.capture_commands(now=now, close=early_close)
    context, flash = commands[0][0], commands[1][0]
    assert context[context.index("--polls") + 1] == "210"
    assert flash[flash.index("--polls") + 1] == "42"
    assert "multi_instrument_signal_v0.py" in commands[2][0][1]


def test_closed_day_starts_no_capture_or_evaluator(tmp_path):
    calls = []
    result = runner.run_session(
        now=OPEN, calendar_row=None, status_path=tmp_path / "status.json",
        popen=lambda *args, **kwargs: calls.append((args, kwargs)),
        run=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert result["status"] == "SKIPPED_MARKET_CLOSED"
    assert result["execution_authority"] is False
    assert calls == []


def test_premarket_starts_only_multi_market_then_rth_jobs(tmp_path):
    launched = []
    before = OPEN - timedelta(hours=5, minutes=30)
    times = iter((OPEN + timedelta(seconds=1), CLOSE, CLOSE + timedelta(seconds=5)))
    result = runner.run_session(
        now=before, calendar_row=CALENDAR, status_path=tmp_path / "status.json",
        popen=lambda command, cwd: (launched.append((command, cwd)) or Child()),
        run=lambda *args, **kwargs: Completed(), sleep=lambda _seconds: None,
        clock=lambda: next(times),
    )
    assert result["status"] == "COMPLETE"
    assert "multi_instrument_signal_v0.py" in launched[0][0][1]
    assert "poll-market" in launched[0][0]
    assert sum("poll-market" in command for command, _cwd in launched) == 1
    assert "market_context_shadow_v0.py" in launched[1][0][1]


def test_session_runs_captures_then_both_evaluators_once(tmp_path):
    launched = []
    evaluated = []
    status = tmp_path / "status.json"

    def popen(command, cwd):
        launched.append((command, cwd))
        return Child()

    def run(command, cwd, check):
        evaluated.append((command, cwd, check))
        return Completed()

    result = runner.run_session(
        now=OPEN + timedelta(seconds=1), calendar_row=CALENDAR, status_path=status,
        popen=popen, run=run, sleep=lambda _seconds: None, clock=lambda: CLOSE,
    )
    assert result["status"] == "COMPLETE"
    assert len(launched) == 4
    assert "market_context_shadow_v0.py" in launched[0][0][1]
    assert "flashalpha_pin_ic_shadow_v0.py" in launched[1][0][1]
    assert len(evaluated) == 4
    assert "prior_regime_flip_reclaim_logger_v0.py" in evaluated[0][0][1]
    assert "intraday_continuation_logger_v0.py" in evaluated[1][0][1]
    assert "multi_instrument_signal_v0.py" in evaluated[2][0][1]
    assert evaluated[3][0][-1] == "report"
    assert json.loads(status.read_text())["status"] == "COMPLETE"


def test_completed_or_running_session_cannot_duplicate(tmp_path):
    for state in ("RUNNING", "COMPLETE"):
        status = tmp_path / f"{state}.json"
        status.write_text(json.dumps({"session_date": "2026-09-08", "status": state}))
        result = runner.run_session(now=OPEN, calendar_row=CALENDAR, status_path=status)
        assert result["status"] == "SKIPPED_ALREADY_STARTED"


def test_launch_artifacts_are_session_scoped_and_have_no_keepalive():
    plist = Path("com.scalpr.signal-studies-session-v0.plist").read_text()
    launcher = Path("run_signal_studies_session_v0.sh").read_text()
    assert "KeepAlive" not in plist and "RunAtLoad" not in plist
    assert "scalp_server.py" not in plist + launcher
    assert "caffeinate -i" in launcher
    assert "signal_study_session_runner_v0.py" in launcher
    assert "<integer>2</integer>" in plist and "<integer>55</integer>" in plist
