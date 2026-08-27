"""Fixture-only tests for nightly PASS/FAIL handling and live path isolation."""

from datetime import datetime, timezone
import json
from pathlib import Path
import plistlib
import subprocess

import a2_relabel_nightly as nightly


NOW = datetime(2026, 8, 27, 2, 0, tzinfo=timezone.utc)


def _paths(tmp_path):
    worktree = tmp_path / "worktree"
    live = tmp_path / "live"
    worktree.mkdir()
    (worktree / "a2_dense_source.py").write_text("# fixture\n")
    (live / ".venv/bin").mkdir(parents=True)
    (live / ".venv/bin/python").write_text("")
    (live / "entry_intelligence_episodes_v1.jsonl").write_text("")
    (live / "tick_log.csv").write_text("provider_ts,symbol\n")
    return nightly.NightlyPaths(worktree, live)


def _completed(payload, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode,
        stdout=json.dumps(payload), stderr=stderr)


def _pass_report():
    return {
        "status": "PASS",
        "checks": {gate: True for gate in nightly.REQUIRED_VERIFY_GATES},
    }


def test_pass_parsing_logs_count_and_uses_only_explicit_live_paths(tmp_path):
    paths = _paths(tmp_path)
    calls = []
    responses = iter([
        _completed({"clean_a2_labelable_episode_count": 7, "records_appended": 2}),
        _completed(_pass_report()),
    ])

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return next(responses)

    notifications = []
    result = nightly.run_nightly(
        paths, now=NOW, runner=runner, notifier=notifications.append)

    assert result["status"] == "PASS"
    assert result["clean_a2_labelable_episode_count"] == 7
    assert notifications == []
    assert str(paths.episodes) in calls[0][0]
    assert str(paths.tick_log) in calls[0][0]
    assert str(paths.quarantine) in calls[0][0]
    assert str(paths.labels) in calls[0][0]
    assert str(paths.summary) in calls[0][0]
    assert str(paths.comparison) in calls[0][0]
    assert str(paths.labels) in calls[1][0]
    assert str(paths.comparison) in calls[1][0]
    assert "clean_a2_labelable_episode_count=7" in Path(result["stdout_log"]).read_text()
    assert Path(result["stderr_log"]).read_text().endswith("\n")


def test_failed_verify_gate_notifies_and_appends_alert_log(tmp_path):
    paths = _paths(tmp_path)
    failed = _pass_report()
    failed["checks"]["every_flip_has_genuine_fresh_dense_quotes"] = False
    responses = iter([
        _completed({"clean_a2_labelable_episode_count": 7, "records_appended": 0}),
        _completed(failed),
    ])

    notifications = []
    result = nightly.run_nightly(
        paths, now=NOW, runner=lambda *_args, **_kwargs: next(responses),
        notifier=notifications.append)

    assert result["status"] == "FAIL"
    assert len(notifications) == 1
    assert "every_flip_has_genuine_fresh_dense_quotes=False" in notifications[0]
    assert notifications[0] in paths.alert_log.read_text()


def test_non_json_or_nonzero_command_is_fail_closed(tmp_path):
    paths = _paths(tmp_path)
    notifications = []
    result = nightly.run_nightly(
        paths, now=NOW,
        runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=2, stdout="not-json", stderr="provider unavailable"),
        notifier=notifications.append)

    assert result["status"] == "FAIL"
    assert result["error"] == "relabel_exit=2"
    assert notifications and "relabel_exit=2" in notifications[0]


def test_shell_wrapper_sources_keychain_and_pins_live_root():
    source = Path(nightly.__file__).with_name("a2_relabel_nightly.sh").read_text()
    assert 'source "$A2_WORKTREE/load_keychain_env.sh"' in source
    assert 'A2_LIVE_ROOT="/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"' in source
    assert '"$A2_LIVE_ROOT/.venv/bin/python"' in source


def test_macos_notification_uses_osascript_without_shell(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(nightly.subprocess, "run", fake_run)
    nightly.macos_notification('gate "failed"')

    assert calls[0][0][0:2] == ["osascript", "-e"]
    assert '\\"failed\\"' in calls[0][0][2]
    assert calls[0][1]["timeout"] == 5.0
    assert calls[0][1]["check"] is False


def test_launchd_plist_is_weekdays_at_2100_and_label_matches_filename():
    path = Path(nightly.__file__).with_name("com.scalpr.a2-relabel-nightly.plist")
    with path.open("rb") as source:
        plist = plistlib.load(source)

    assert path.stem == plist["Label"]
    assert plist["Label"] == "com.scalpr.a2-relabel-nightly"
    schedule = plist["StartCalendarInterval"]
    assert schedule == [
        {"Weekday": weekday, "Hour": 21, "Minute": 0}
        for weekday in range(2, 7)
    ]
