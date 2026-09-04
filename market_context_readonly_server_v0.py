#!/usr/bin/env python3
"""Local read-only bridge from the context JSONL ledger to dashboard.html."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_LEDGER = Path("market_context_shadow_v0.jsonl")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8421


def read_ledger_state(path: Path) -> tuple[str, dict[str, Any] | None, int]:
    """Return latest context observation and valid-record count without writes."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return "MISSING", None, 0
    except OSError:
        return "UNREADABLE", None, 0

    latest = None
    count = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("record_type") == "MARKET_CONTEXT_OBSERVATION":
            latest = record
            count += 1
    return ("AVAILABLE", latest, count) if latest is not None else ("EMPTY", None, 0)


def read_latest_record(path: Path) -> tuple[str, dict[str, Any] | None]:
    status, record, _ = read_ledger_state(path)
    return status, record


def response_payload(path: Path) -> dict[str, Any]:
    status, record, record_count = read_ledger_state(path)
    return {
        "execution_authority": False,
        "is_inferential": False,
        "ledger_status": status,
        "record": record,
        "record_count": record_count,
        "study_status": "EXPLORATORY_NON_INFERENTIAL",
    }


def make_handler(ledger: Path) -> type[BaseHTTPRequestHandler]:
    class ReadOnlyContextHandler(BaseHTTPRequestHandler):
        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path == "/latest":
                self._send_json(HTTPStatus.OK, response_payload(ledger))
                return
            if self.path == "/health":
                self._send_json(HTTPStatus.OK, {
                    "execution_authority": False,
                    "service": "market-context-readonly-v0",
                    "status": "READ_ONLY",
                })
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802 - explicitly reject writes
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only"})

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

        def log_message(self, format: str, *args: object) -> None:
            return

    return ReadOnlyContextHandler


def serve(*, ledger: Path, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(ledger))
    print(
        json.dumps({
            "execution_authority": False,
            "ledger": str(ledger.resolve()),
            "listen": f"http://{host}:{port}",
            "mode": "READ_ONLY",
        }, sort_keys=True),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    serve(ledger=args.ledger, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
