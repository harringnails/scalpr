"""Read-only collector liveness alert.

Market-aware, stdlib-only detector for the Entry Intelligence collector.
It reads the collector status and file mtimes, emits macOS notifications on
alert transitions, and appends alert/recovery events to a dedicated log.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATUS_PATH = ROOT / "entry_intelligence_collector_status_v1.json"
DECISION_FILES = (
    ROOT / "entry_intelligence_decisions_v1.jsonl",
    ROOT / "entry_intelligence_decision_events_v1.jsonl",
    ROOT / "entry_intelligence_gate_results_v1.jsonl",
)
ALERT_LOG_PATH = ROOT / "collector_alerts_v0.log"
STATE_PATH = ROOT / "collector_alerts_v0.state.json"

RTH_STALE_SECONDS = 300
HEARTBEAT_STALE_SECONDS = 300
DEDUP_ALERT_SECONDS = 900
STATE_READ_RETRIES = 3
STATE_READ_RETRY_SECONDS = 0.05
TRANSIENT_READ_ERRNOS = {errno.EAGAIN, errno.EDEADLK}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(ts: float | None = None) -> str:
    moment = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return moment.isoformat()


def _parse_status_payload(text: str) -> dict[str, object]:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("collector status payload is not an object")
    return payload


def _read_text_with_retry(path: Path) -> str:
    for attempt in range(STATE_READ_RETRIES):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            retryable = exc.errno in TRANSIENT_READ_ERRNOS and attempt + 1 < STATE_READ_RETRIES
            if not retryable:
                raise
            time.sleep(STATE_READ_RETRY_SECONDS)
    raise RuntimeError("unreachable")


def load_status(path: Path = STATUS_PATH) -> dict[str, object]:
    return _parse_status_payload(_read_text_with_retry(path))


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def newest_decision_write(paths: tuple[Path, ...] = DECISION_FILES) -> float | None:
    mtimes = [stamp for path in paths if (stamp := _mtime(path)) is not None]
    return max(mtimes) if mtimes else None


def _parse_timestamp(value: object) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _status_heartbeat_age_seconds(
    status: dict[str, object],
    *,
    now: float,
    status_path: Path = STATUS_PATH,
) -> float | None:
    updated = _parse_timestamp(status.get("updated_at"))
    if updated is not None:
        return now - updated
    mtime = _mtime(status_path)
    return None if mtime is None else now - mtime


def evaluate_alert(
    *,
    status_path: Path = STATUS_PATH,
    decision_paths: tuple[Path, ...] = DECISION_FILES,
    now: float | None = None,
) -> dict[str, object]:
    now = time.time() if now is None else float(now)
    try:
        status = load_status(status_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        age = None
        if (mtime := _mtime(status_path)) is not None:
            age = now - mtime
        return {
            "alert": bool(age is None or age > HEARTBEAT_STALE_SECONDS),
            "kind": "collector_status_unreadable",
            "state": "UNKNOWN",
            "market_window": None,
            "age_seconds": age,
            "message": f"ALERT: collector status unreadable ({type(exc).__name__})",
        }

    state = str(status.get("state") or "UNKNOWN")
    market_window = bool(status.get("market_window"))
    heartbeat_age = _status_heartbeat_age_seconds(status, now=now, status_path=status_path)
    newest_write = newest_decision_write(decision_paths)
    write_age = None if newest_write is None else now - newest_write
    in_rth = state == "ACTIVE_RTH_CAPTURE" or market_window is True

    if in_rth:
        if write_age is not None and write_age <= RTH_STALE_SECONDS:
            return {
                "alert": False,
                "kind": "ok",
                "state": state,
                "market_window": market_window,
                "age_seconds": write_age,
                "message": "OK: collector producing during RTH",
            }
        stale_age = write_age if write_age is not None else None
        return {
            "alert": True,
            "kind": "rth_no_production",
            "state": state,
            "market_window": market_window,
            "age_seconds": stale_age,
            "message": "ALERT: collector not producing during RTH",
        }

    if heartbeat_age is not None and heartbeat_age <= HEARTBEAT_STALE_SECONDS:
        return {
            "alert": False,
            "kind": "ok",
            "state": state,
            "market_window": market_window,
            "age_seconds": heartbeat_age,
            "message": "OK: collector heartbeat fresh while market closed",
        }
    return {
        "alert": True,
        "kind": "collector_heartbeat_stale",
        "state": state,
        "market_window": market_window,
        "age_seconds": heartbeat_age,
        "message": "ALERT: collector heartbeat stale",
    }


def _load_state(path: Path = STATE_PATH) -> dict[str, object]:
    try:
        payload = json.loads(_read_text_with_retry(path))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_state(state: dict[str, object], path: Path = STATE_PATH) -> None:
    path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _append_alert_log(record: dict[str, object], path: Path = ALERT_LOG_PATH) -> None:
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{message}" with title "{title}" sound name "Basso"',
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return


def _transition_kind(previous: str | None, current: str) -> str | None:
    if current == "ok" and previous and previous != "ok":
        return "RECOVERED"
    if current != "ok" and previous != current:
        return "ALERT"
    if current != "ok":
        return "ALERT_REPEAT"
    return None


def run_once(
    *,
    status_path: Path = STATUS_PATH,
    decision_paths: tuple[Path, ...] = DECISION_FILES,
    alert_log_path: Path = ALERT_LOG_PATH,
    state_path: Path = STATE_PATH,
    now: float | None = None,
    notify: bool = True,
) -> dict[str, object]:
    observation = evaluate_alert(status_path=status_path, decision_paths=decision_paths, now=now)
    prior = _load_state(state_path)
    previous_kind = prior.get("current_kind") if isinstance(prior, dict) else None
    transition = _transition_kind(str(previous_kind) if previous_kind is not None else None, str(observation["kind"]))
    alerting = bool(observation["alert"])
    event = None

    if transition == "ALERT" or transition == "RECOVERED":
        event = transition
    elif transition == "ALERT_REPEAT":
        last_notify_at = float(prior.get("last_notify_at", 0.0) or 0.0) if isinstance(prior, dict) else 0.0
        current_now = time.time() if now is None else float(now)
        if current_now - last_notify_at >= DEDUP_ALERT_SECONDS:
            event = "ALERT"

    if event == "ALERT" and alerting:
        record = {
            "ts": iso_utc(now),
            "event": "ALERT",
            "kind": observation["kind"],
            "state": observation["state"],
            "market_window": observation["market_window"],
            "age_seconds": observation["age_seconds"],
            "message": observation["message"],
        }
        _append_alert_log(record, alert_log_path)
        if notify:
            _notify("Scalpr collector ALERT", str(observation["message"]))
        _save_state({
            "current_kind": observation["kind"],
            "last_event": "ALERT",
            "last_notify_at": time.time() if now is None else float(now),
        }, state_path)
        print(observation["message"])
        return {**observation, "event": "ALERT", "notified": True}

    if event == "RECOVERED":
        record = {
            "ts": iso_utc(now),
            "event": "RECOVERED",
            "kind": observation["kind"],
            "state": observation["state"],
            "market_window": observation["market_window"],
            "age_seconds": observation["age_seconds"],
            "message": observation["message"],
        }
        _append_alert_log(record, alert_log_path)
        if notify:
            _notify("Scalpr collector RECOVERED", str(observation["message"]))
        _save_state({
            "current_kind": observation["kind"],
            "last_event": "RECOVERED",
            "last_notify_at": time.time() if now is None else float(now),
        }, state_path)
        print(observation["message"])
        return {**observation, "event": "RECOVERED", "notified": True}

    _save_state({
        "current_kind": observation["kind"],
        "last_event": prior.get("last_event") if isinstance(prior, dict) else None,
        "last_notify_at": prior.get("last_notify_at") if isinstance(prior, dict) else None,
    }, state_path)
    print(observation["message"])
    return {**observation, "event": None, "notified": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only collector liveness alert")
    parser.add_argument("--once", action="store_true", help="Run one poll and exit")
    parser.add_argument("--status-path", type=Path, default=STATUS_PATH)
    parser.add_argument("--alert-log-path", type=Path, default=ALERT_LOG_PATH)
    parser.add_argument("--state-path", type=Path, default=STATE_PATH)
    args = parser.parse_args(argv)
    run_once(
        status_path=args.status_path,
        alert_log_path=args.alert_log_path,
        state_path=args.state_path,
        notify=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
