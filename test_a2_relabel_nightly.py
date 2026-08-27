"""Fixture-only tests for nightly PASS/FAIL handling and live path isolation."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import plistlib
import subprocess

import a2_dense_source as dense
import a2_measurement as a2
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
    assert Path(result["stderr_log"]).read_text() == ""
    assert paths.labels.name == "a2_labels_dense_v0.jsonl"
    assert paths.summary.name == "a2_summary_dense_v0.json"
    assert paths.comparison.name == "a2_dense_source_comparison_v0.json"
    assert paths.labels != paths.live_root / "v2_data/a2_measurement/a2_labels_v2.jsonl"
    assert paths.summary != paths.live_root / "v2_data/a2_measurement/a2_summary_v2.json"


def test_keychain_loader_is_bounded_and_never_uses_a_shell():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        service = command[command.index("-s") + 1]
        secret = "paper-key" if service.endswith(".key") else "paper-secret"
        return subprocess.CompletedProcess(command, 0, secret + "\n", "")

    credentials = nightly.load_alpaca_credentials_from_keychain(
        account="natalie", timeout_seconds=4.0, runner=runner)

    assert credentials == {
        "ALPACA_API_KEY": "paper-key",
        "ALPACA_SECRET_KEY": "paper-secret",
    }
    assert len(calls) == 2
    assert all(call[0][0] == "/usr/bin/security" for call in calls)
    assert all(call[1]["timeout"] == 4.0 for call in calls)
    assert all("shell" not in call[1] for call in calls)


def test_nightly_requires_keychain_and_does_not_log_secrets(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.scalpr.a2-relabel-nightly")
    calls = []
    responses = iter([
        _completed({"clean_a2_labelable_episode_count": 7, "records_appended": 0}),
        _completed(_pass_report()),
    ])

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return next(responses)

    result = nightly.run_nightly(
        paths, now=NOW, runner=runner, notifier=lambda _message: None,
        credential_loader=lambda: {
            "ALPACA_API_KEY": "do-not-log-key",
            "ALPACA_SECRET_KEY": "do-not-log-secret",
        })

    assert result["status"] == "PASS"
    assert result["credential_source"] == "macos_keychain_security_cli"
    assert result["launchd_service"] == "com.scalpr.a2-relabel-nightly"
    assert calls[0][1]["environment"]["ALPACA_API_KEY"] == "do-not-log-key"
    logged = Path(result["stdout_log"]).read_text() + Path(result["stderr_log"]).read_text()
    assert "launchd_service=com.scalpr.a2-relabel-nightly" in logged
    assert "credential_source=macos_keychain_security_cli" in logged
    assert "do-not-log-key" not in logged
    assert "do-not-log-secret" not in logged


def test_keychain_failure_alerts_and_stops_before_dense_commands(tmp_path):
    paths = _paths(tmp_path)
    calls = []

    def unavailable():
        raise nightly.KeychainCredentialError(
            "keychain_item_unavailable_service=scalpr.alpaca.paper.key exit=44")

    result = nightly.run_nightly(
        paths, now=NOW, runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        notifier=lambda _message: None, credential_loader=unavailable)

    assert result["status"] == "FAIL"
    assert "keychain_item_unavailable" in result["error"]
    assert calls == []


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


def test_manual_shell_wrapper_delegates_without_sourcing_credentials():
    source = Path(nightly.__file__).with_name("a2_relabel_nightly.sh").read_text()
    assert "load_keychain_env.sh" not in source
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
    arguments = plist["ProgramArguments"]
    assert arguments == [
        "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/.venv/bin/python",
        "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-a2-relabel/a2_relabel_nightly.py",
        "--worktree",
        "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-a2-relabel",
        "--live-root",
        "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7",
    ]
    assert "/bin/bash" not in arguments
    assert plist["WorkingDirectory"] == "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
    schedule = plist["StartCalendarInterval"]
    assert schedule == [
        {"Weekday": weekday, "Hour": 21, "Minute": 0}
        for weekday in range(2, 7)
    ]


class _WindowClient:
    def historical_quotes(self, symbol, *, start, end):
        boundary = end - timedelta(microseconds=1)
        return [{
            "provider_ts": boundary - timedelta(seconds=1),
            "received_at": None,
            "mid": 101.0,
            "bid": 100.99,
            "ask": 101.01,
            "source": a2.DENSE_ENDPOINT_SOURCE,
            "endpoint_source": a2.DENSE_ENDPOINT_SOURCE,
        }]


def test_wrapper_dense_outputs_survive_subsequent_legacy_materialize(tmp_path):
    paths = _paths(tmp_path)
    paths.episodes.write_text(json.dumps({
        "episode_key": "wrapper-proof",
        "episode_record_id": "record-wrapper-proof",
        "decision_id": "decision-wrapper-proof",
        "cohort_id": "low_reversal_v1",
        "config_version": "entry-intelligence-config-v1.2.0",
        "config_hash": "frozen-config-hash",
        "symbol": "SPY",
        "side": "CALL",
        "session_date": "2026-08-14",
        "decided_at": "2026-08-14T15:55:00+00:00",
        "admitted": True,
    }) + "\n")

    def runner(command, **_kwargs):
        if "relabel" in command:
            payload = dense.run_uniform_relabel(
                _WindowClient(), episodes_path=paths.episodes,
                tick_log_path=paths.tick_log, quarantine_path=paths.quarantine,
                output_path=paths.labels, summary_path=paths.summary,
                comparison_path=paths.comparison)
        else:
            payload = dense.verify_uniform_relabel(
                labels_path=paths.labels, comparison_path=paths.comparison)
        return _completed(payload)

    result = nightly.run_nightly(
        paths, now=NOW, runner=runner, notifier=lambda _message: None)
    dense_summary = json.loads(paths.summary.read_text())
    dense_comparison = json.loads(paths.comparison.read_text())
    dense_before = {
        path: path.read_bytes()
        for path in (paths.labels, paths.summary, paths.comparison)
    }

    legacy_labels = paths.live_root / "v2_data/a2_measurement/a2_labels_v2.jsonl"
    legacy_summary = paths.live_root / "v2_data/a2_measurement/a2_summary_v2.json"
    a2.materialize_a2(
        episodes_path=paths.episodes, tick_log_path=paths.tick_log,
        quarantine_path=paths.quarantine, output_path=legacy_labels,
        summary_path=legacy_summary)

    assert result["status"] == "PASS"
    assert dense_summary["endpoint_source"] == a2.DENSE_ENDPOINT_SOURCE
    assert dense_summary["clean_a2_labelable_episode_count"] == 1
    assert dense_comparison["dense_available_count"] == 1
    assert all(path.read_bytes() == before for path, before in dense_before.items())
    assert legacy_summary.exists()
