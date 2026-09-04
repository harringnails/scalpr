#!/usr/bin/env python3
"""Run the isolated context and signal-study capture lifecycle for one session."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import subprocess
import sys
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests


ET = ZoneInfo("America/New_York")
ALPACA_CALENDAR_URL = "https://paper-api.alpaca.markets/v2/calendar"
LIVE_ROOT = Path("/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7")
FLASH_ROOT = Path("/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-flashalpha-pin-scanner")
PYTHON = LIVE_ROOT / ".venv/bin/python"
FLASH_LEDGER = FLASH_ROOT / "flashalpha_pin_study_v0.jsonl"
STATUS_PATH = LIVE_ROOT / "signal_study_session_status_v0.json"
LOCK_PATH = Path("/tmp/com.scalpr.signal-studies-session-v0.lock")
CONTEXT_INTERVAL_SECONDS = 60
FLASH_INTERVAL_SECONDS = 300
MULTI_MARKET_INTERVAL_SECONDS = 5


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_status(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def market_calendar(
    session_date: date,
    *,
    api_key: str,
    api_secret: str,
    requester: Callable[..., Any] = requests.get,
) -> dict[str, Any] | None:
    response = requester(
        f"{ALPACA_CALENDAR_URL}?{urlencode({'start': session_date.isoformat(), 'end': session_date.isoformat()})}",
        headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        return None
    row = payload[0]
    return row if isinstance(row, dict) and row.get("date") == session_date.isoformat() else None


def session_bounds(row: dict[str, Any]) -> tuple[datetime, datetime]:
    session_date = date.fromisoformat(str(row["date"]))

    def parse_boundary(value: Any) -> datetime:
        text = str(value)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.combine(session_date, clock_time.fromisoformat(text), ET)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return parsed.astimezone(timezone.utc)

    opened = parse_boundary(row["open"])
    closed = parse_boundary(row["close"])
    if closed <= opened:
        raise ValueError("market calendar close must be after open")
    return opened, closed


def remaining_polls(now: datetime, close: datetime, interval_seconds: int) -> int:
    remaining = (close - now).total_seconds()
    return max(0, math.ceil(remaining / interval_seconds))


def multi_market_command(*, now: datetime, close: datetime) -> tuple[list[str], Path] | None:
    market_polls = remaining_polls(now, close, MULTI_MARKET_INTERVAL_SECONDS)
    if not market_polls:
        return None
    return ([str(PYTHON), str(LIVE_ROOT / "multi_instrument_signal_v0.py"), "poll-market",
             "--polls", str(market_polls), "--interval-seconds", str(MULTI_MARKET_INTERVAL_SECONDS)], LIVE_ROOT)


def capture_commands(*, now: datetime, close: datetime, include_multi_market: bool = True) -> list[tuple[list[str], Path]]:
    context_polls = remaining_polls(now, close, CONTEXT_INTERVAL_SECONDS)
    flash_polls = remaining_polls(now, close, FLASH_INTERVAL_SECONDS)
    commands: list[tuple[list[str], Path]] = []
    if context_polls:
        commands.append(([
            str(PYTHON), str(LIVE_ROOT / "market_context_shadow_v0.py"), "capture",
            "--polls", str(context_polls), "--interval-seconds", str(CONTEXT_INTERVAL_SECONDS),
            "--flashalpha-ledger", str(FLASH_LEDGER),
        ], LIVE_ROOT))
    if flash_polls:
        commands.append(([
            str(PYTHON), str(FLASH_ROOT / "flashalpha_pin_ic_shadow_v0.py"), "study-poll",
            "--polls", str(flash_polls), "--interval-seconds", str(FLASH_INTERVAL_SECONDS),
        ], FLASH_ROOT))
        commands.append(([
            str(PYTHON), str(LIVE_ROOT / "multi_instrument_signal_v0.py"), "poll-flash",
            "--polls", str(flash_polls), "--interval-seconds", str(FLASH_INTERVAL_SECONDS),
        ], LIVE_ROOT))
    market = multi_market_command(now=now, close=close) if include_multi_market else None
    if market:
        commands.append(market)
    return commands


def evaluator_commands(session_date: str) -> list[list[str]]:
    shared = ["--session-date", session_date, "--tick-log", str(LIVE_ROOT / "tick_log.csv"),
              "--flashalpha-ledger", str(FLASH_LEDGER)]
    return [
        [str(PYTHON), str(LIVE_ROOT / "prior_regime_flip_reclaim_logger_v0.py"), "evaluate", *shared],
        [str(PYTHON), str(LIVE_ROOT / "intraday_continuation_logger_v0.py"), "evaluate", *shared],
        [str(PYTHON), str(LIVE_ROOT / "multi_instrument_signal_v0.py"), "evaluate",
         "--session-date", session_date],
        [str(PYTHON), str(LIVE_ROOT / "multi_instrument_signal_v0.py"), "report"],
    ]


def run_session(
    *,
    now: datetime,
    calendar_row: dict[str, Any] | None,
    status_path: Path = STATUS_PATH,
    popen: Callable[..., Any] = subprocess.Popen,
    run: Callable[..., Any] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    session_date = now.astimezone(ET).date().isoformat()
    authority = {"execution_authority": False, "admission_authority": False}
    if calendar_row is None:
        result = {**authority, "session_date": session_date, "status": "SKIPPED_MARKET_CLOSED"}
        atomic_json(status_path, result)
        return result

    session_date = str(calendar_row["date"])
    opened, closed = session_bounds(calendar_row)
    prior = read_status(status_path)
    if prior.get("session_date") == session_date and prior.get("status") in {"RUNNING", "COMPLETE"}:
        return {**authority, "session_date": session_date, "status": "SKIPPED_ALREADY_STARTED"}

    early_market_child = None
    if now < opened:
        market = multi_market_command(now=now, close=closed)
        if market:
            early_market_child = popen(market[0], cwd=market[1])
        sleep((opened - now).total_seconds() + 1.0)
        now = clock()
    if now >= closed:
        result = {**authority, "session_date": session_date, "status": "MISSED_SESSION"}
        atomic_json(status_path, result)
        return result

    atomic_json(status_path, {
        **authority, "market_close_utc": closed.isoformat(), "market_open_utc": opened.isoformat(),
        "session_date": session_date, "started_at_utc": now.isoformat(), "status": "RUNNING",
    })
    children = ([early_market_child] if early_market_child else []) + [
        popen(command, cwd=cwd) for command, cwd in capture_commands(
            now=now, close=closed, include_multi_market=early_market_child is None)]
    capture_codes = [child.wait() for child in children]
    remaining = (closed - clock()).total_seconds()
    if remaining > 0:
        sleep(remaining + 5.0)

    evaluator_codes = [run(command, cwd=LIVE_ROOT, check=False).returncode
                       for command in evaluator_commands(session_date)]
    complete = all(code == 0 for code in capture_codes + evaluator_codes)
    result = {
        **authority,
        "capture_exit_codes": capture_codes,
        "completed_at_utc": clock().isoformat(),
        "evaluator_exit_codes": evaluator_codes,
        "session_date": session_date,
        "status": "COMPLETE" if complete else "FAILED",
    }
    atomic_json(status_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run-session",))
    parser.add_argument("--status", type=Path, default=STATUS_PATH)
    args = parser.parse_args()

    api_key = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not api_secret:
        raise SystemExit("Alpaca Keychain environment is unavailable")
    now = datetime.now(timezone.utc)
    row = market_calendar(now.astimezone(ET).date(), api_key=api_key, api_secret=api_secret)
    LOCK_PATH.touch(exist_ok=True)
    with LOCK_PATH.open("r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"execution_authority": False, "status": "SKIPPED_LOCKED"}))
            return 0
        result = run_session(now=now, calendar_row=row, status_path=args.status)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"COMPLETE", "SKIPPED_MARKET_CLOSED", "SKIPPED_ALREADY_STARTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
