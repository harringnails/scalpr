#!/usr/bin/env python3
"""Local read-only HTTP bridge for point-in-time option scenario arithmetic."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from option_scenario_v0 import LiveScenarioSource


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8422


def unavailable(reason: str) -> dict[str, Any]:
    return {
        "admission_authority": False, "breakeven": None, "execution_authority": False,
        "expected_move": None, "is_forecast": False,
        "label": "mechanical scenarios given an assumed move — not a forecast or a trade signal.",
        "reason": reason, "scenario_rows": [], "status": "UNAVAILABLE",
    }


def make_handler(source_factory: Callable[[], Any]) -> type[BaseHTTPRequestHandler]:
    class ReadOnlyScenarioHandler(BaseHTTPRequestHandler):
        def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self.send_json(HTTPStatus.OK, {"execution_authority": False, "status": "READ_ONLY"})
                return
            if parsed.path != "/scenario":
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            query = parse_qs(parsed.query)
            symbol = str((query.get("symbol") or [""])[0]).upper()
            try:
                quantity = int((query.get("qty") or ["1"])[0])
            except ValueError:
                quantity = 0
            try:
                payload = source_factory().fetch(symbol, quantity, datetime.now(timezone.utc))
            except Exception as exc:
                payload = unavailable(type(exc).__name__)
            self.send_json(HTTPStatus.OK, payload)

        def do_POST(self) -> None:  # noqa: N802
            self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only"})

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

        def log_message(self, format: str, *args: object) -> None:
            return

    return ReadOnlyScenarioHandler


def source_from_environment() -> LiveScenarioSource:
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Alpaca Keychain environment unavailable")
    return LiveScenarioSource(key, secret)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(source_from_environment))
    print(json.dumps({"execution_authority": False, "listen": f"http://{args.host}:{args.port}", "mode": "READ_ONLY"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
