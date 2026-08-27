"""Nightly orchestration for the standalone, read-only A2 dense re-label."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


DEFAULT_WORKTREE = Path("/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-a2-relabel")
DEFAULT_LIVE_ROOT = Path("/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7")
DEFAULT_JOB_TIMEOUT_SECONDS = 1800.0
DEFAULT_KEYCHAIN_TIMEOUT_SECONDS = 30.0
SECURITY_BIN = Path("/usr/bin/security")
ALPACA_KEYCHAIN_ITEMS = {
    "ALPACA_API_KEY": "scalpr.alpaca.paper.key",
    "ALPACA_SECRET_KEY": "scalpr.alpaca.paper.secret",
}
REQUIRED_VERIFY_GATES = (
    "all_dense_run_labels_present",
    "provenance_and_frozen_rules_intact",
    "availability_rises_vs_tick_log",
    "every_flip_has_genuine_fresh_dense_quotes",
)


class KeychainCredentialError(RuntimeError):
    """The nightly process could not read its required Keychain items."""


@dataclass(frozen=True)
class NightlyPaths:
    worktree: Path
    live_root: Path

    @property
    def python(self) -> Path:
        return self.live_root / ".venv/bin/python"

    @property
    def dense_script(self) -> Path:
        return self.worktree / "a2_dense_source.py"

    @property
    def episodes(self) -> Path:
        return self.live_root / "entry_intelligence_episodes_v1.jsonl"

    @property
    def tick_log(self) -> Path:
        return self.live_root / "tick_log.csv"

    @property
    def quarantine(self) -> Path:
        return self.live_root / "entry_intelligence_episode_quarantine_v1.jsonl"

    @property
    def labels(self) -> Path:
        return self.live_root / "v2_data/a2_measurement/a2_labels_dense_v0.jsonl"

    @property
    def summary(self) -> Path:
        return self.live_root / "v2_data/a2_measurement/a2_summary_dense_v0.json"

    @property
    def comparison(self) -> Path:
        return self.live_root / "v2_data/a2_measurement/a2_dense_source_comparison_v0.json"

    @property
    def log_dir(self) -> Path:
        return self.live_root / "v2_data/a2_measurement/nightly_logs"

    @property
    def alert_log(self) -> Path:
        return self.live_root / "a2_relabel_alerts.log"


def relabel_command(paths: NightlyPaths) -> list[str]:
    return [
        str(paths.python), str(paths.dense_script), "relabel",
        "--episodes", str(paths.episodes),
        "--tick-log", str(paths.tick_log),
        "--quarantine", str(paths.quarantine),
        "--labels", str(paths.labels),
        "--summary", str(paths.summary),
        "--comparison", str(paths.comparison),
    ]


def verify_command(paths: NightlyPaths) -> list[str]:
    return [
        str(paths.python), str(paths.dense_script), "verify",
        "--labels", str(paths.labels),
        "--comparison", str(paths.comparison),
    ]


def parse_json_output(output: str, *, command: str) -> dict[str, Any]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{command} returned non-JSON output") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{command} returned a non-object JSON payload")
    return payload


def load_alpaca_credentials_from_keychain(
    *,
    account: str | None = None,
    timeout_seconds: float = DEFAULT_KEYCHAIN_TIMEOUT_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    """Read only the Alpaca pair from Keychain, with no environment fallback."""
    account = (account or getpass.getuser()).strip()
    if not account:
        raise KeychainCredentialError("keychain_account_unavailable")
    credentials: dict[str, str] = {}
    for environment_name, service in ALPACA_KEYCHAIN_ITEMS.items():
        command = [
            str(SECURITY_BIN), "find-generic-password",
            "-a", account, "-s", service, "-w",
        ]
        try:
            result = runner(
                command, capture_output=True, text=True,
                timeout=timeout_seconds, check=False)
        except subprocess.TimeoutExpired as exc:
            raise KeychainCredentialError(
                f"keychain_timeout_service={service}"
            ) from exc
        except OSError as exc:
            raise KeychainCredentialError(
                f"keychain_launch_error_service={service}: {type(exc).__name__}"
            ) from exc
        secret = (result.stdout or "").strip()
        if result.returncode != 0 or not secret:
            raise KeychainCredentialError(
                f"keychain_item_unavailable_service={service} "
                f"exit={result.returncode}"
            )
        credentials[environment_name] = secret
    return credentials


def verify_passed(report: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if report.get("status") != "PASS":
        failures.append(f"status={report.get('status')!r}")
    checks = report.get("checks")
    if not isinstance(checks, dict):
        return False, [*failures, "checks_missing"]
    for gate in REQUIRED_VERIFY_GATES:
        if checks.get(gate) is not True:
            failures.append(f"{gate}={checks.get(gate)!r}")
    return not failures, failures


def _apple_script_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def macos_notification(message: str) -> None:
    script = (
        f'display notification "{_apple_script_string(message)}" '
        'with title "Scalpr A2 nightly re-label"'
    )
    subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
        timeout=5.0, check=False)


def append_alert(path: Path, message: str, *, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as target:
        target.write(f"{now.astimezone(timezone.utc).isoformat()} {message}\n")


def _run_command(
    command: list[str], *, cwd: Path, timeout: float,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True,
        timeout=timeout, check=False, env=environment)


def run_nightly(
    paths: NightlyPaths,
    *,
    now: datetime | None = None,
    timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run_command,
    notifier: Callable[[str], None] = macos_notification,
    credential_loader: Callable[[], dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Run relabel then verify; alert on any command, parse, or gate failure."""
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = paths.log_dir / f"a2_relabel_{stamp}.out.log"
    stderr_path = paths.log_dir / f"a2_relabel_{stamp}.err.log"
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def finish_failure(message: str) -> dict[str, Any]:
        alert = f"FAIL: {message}"
        append_alert(paths.alert_log, alert, now=now)
        try:
            notifier(alert)
        except Exception as exc:
            stderr_parts.append(f"notification_error={type(exc).__name__}: {exc}\n")
        stdout_path.write_text("".join(stdout_parts), encoding="utf-8")
        stderr_path.write_text("".join(stderr_parts), encoding="utf-8")
        return {
            "status": "FAIL",
            "error": message,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
        }

    for required in (paths.python, paths.dense_script, paths.episodes, paths.tick_log):
        if not required.exists():
            return finish_failure(f"required_path_missing={required}")

    command_environment: dict[str, str] | None = None
    credential_source: str | None = None
    launchd_service = os.environ.get("XPC_SERVICE_NAME")
    if launchd_service:
        stdout_parts.append(f"launchd_service={launchd_service}\n")
    if credential_loader is not None:
        try:
            credentials = credential_loader()
        except KeychainCredentialError as exc:
            return finish_failure(str(exc))
        except Exception as exc:
            return finish_failure(
                f"keychain_unexpected_error={type(exc).__name__}")
        missing = sorted(set(ALPACA_KEYCHAIN_ITEMS) - set(credentials))
        if missing:
            return finish_failure(
                "keychain_credentials_incomplete=" + ",".join(missing))
        command_environment = os.environ.copy()
        command_environment.update(credentials)
        credential_source = "macos_keychain_security_cli"
        stdout_parts.append(f"credential_source={credential_source}\n")

    try:
        relabel = runner(
            relabel_command(paths), cwd=paths.worktree,
            timeout=timeout_seconds, environment=command_environment)
    except subprocess.TimeoutExpired:
        return finish_failure(f"relabel_timeout_seconds={timeout_seconds:g}")
    except Exception as exc:
        return finish_failure(f"relabel_launch_error={type(exc).__name__}: {exc}")
    stdout_parts.append("=== RELABEL STDOUT ===\n" + (relabel.stdout or "") + "\n")
    if relabel.stderr:
        stderr_parts.append("=== RELABEL STDERR ===\n" + relabel.stderr + "\n")
    if relabel.returncode != 0:
        return finish_failure(f"relabel_exit={relabel.returncode}")
    try:
        relabel_report = parse_json_output(relabel.stdout, command="relabel")
    except ValueError as exc:
        return finish_failure(str(exc))

    try:
        verify = runner(
            verify_command(paths), cwd=paths.worktree,
            timeout=timeout_seconds, environment=command_environment)
    except subprocess.TimeoutExpired:
        return finish_failure(f"verify_timeout_seconds={timeout_seconds:g}")
    except Exception as exc:
        return finish_failure(f"verify_launch_error={type(exc).__name__}: {exc}")
    stdout_parts.append("=== VERIFY STDOUT ===\n" + (verify.stdout or "") + "\n")
    if verify.stderr:
        stderr_parts.append("=== VERIFY STDERR ===\n" + verify.stderr + "\n")
    if verify.returncode != 0:
        return finish_failure(f"verify_exit={verify.returncode}")
    try:
        verify_report = parse_json_output(verify.stdout, command="verify")
    except ValueError as exc:
        return finish_failure(str(exc))
    passed, failures = verify_passed(verify_report)
    if not passed:
        return finish_failure("verify_gates_failed=" + ",".join(failures))

    count = relabel_report.get("clean_a2_labelable_episode_count")
    if not isinstance(count, int) or count < 0:
        return finish_failure(f"invalid_clean_labelable_episode_count={count!r}")
    appended = relabel_report.get("records_appended")
    success = (
        f"SUCCESS verify=PASS clean_a2_labelable_episode_count={count} "
        f"records_appended={appended}"
    )
    stdout_parts.append(success + "\n")
    stdout_path.write_text("".join(stdout_parts), encoding="utf-8")
    stderr_path.write_text("".join(stderr_parts), encoding="utf-8")
    return {
        "status": "PASS",
        "clean_a2_labelable_episode_count": count,
        "records_appended": appended,
        "credential_source": credential_source,
        "launchd_service": launchd_service,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, default=DEFAULT_WORKTREE)
    parser.add_argument("--live-root", type=Path, default=DEFAULT_LIVE_ROOT)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_JOB_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    report = run_nightly(
        NightlyPaths(args.worktree.resolve(), args.live_root.resolve()),
        timeout_seconds=args.timeout_seconds,
        credential_loader=load_alpaca_credentials_from_keychain)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
