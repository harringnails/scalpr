#!/usr/bin/env python3
"""Read-only Alpaca candle and FlashAlpha structure bridge for the dashboard."""

from __future__ import annotations

import argparse
import json
import os
import threading
from datetime import datetime, time, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


SYMBOLS = ("SPY", "QQQ", "IWM", "DIA")
TIMEFRAMES = ("1m", "5m")
ET = ZoneInfo("America/New_York")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8423
DEFAULT_STRUCTURE_LEDGER = Path("multi_instrument_flashalpha_v0.jsonl")
DEFAULT_MULTI_EPISODES = Path("multi_instrument_signal_v0.jsonl")
DEFAULT_SPY_A = Path("prior_regime_flip_reclaim_v0.jsonl")
DEFAULT_SPY_B = Path("intraday_continuation_v0.jsonl")
DEFAULT_ECHARTS = Path("vendor/echarts.min.js")
STRUCTURE_MAX_AGE_SECONDS = 360.0
BAR_MAX_AGE_SECONDS = {"1m": 180.0, "5m": 480.0}
LABEL = (
    "shadow · non-inferential · candles are Alpaca trade OHLC (display), "
    "study basis is quote-mid · no execution/admission/Guard authority."
)


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return rows
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


class LedgerCache:
    """Cache immutable projections by mtime; source ledgers are never opened for writes."""

    def __init__(self) -> None:
        self._values: dict[Path, tuple[int, list[dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def read(self, path: Path) -> list[dict[str, Any]]:
        try:
            modified = path.stat().st_mtime_ns
        except OSError:
            return []
        with self._lock:
            cached = self._values.get(path)
            if cached and cached[0] == modified:
                return cached[1]
            rows = read_jsonl(path)
            self._values[path] = (modified, rows)
            return rows


def latest_structure(rows: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    candidates = [row for row in rows if row.get("record_type") == "MULTI_INSTRUMENT_STRUCTURE"
                  and row.get("symbol") == symbol]
    return max(candidates, key=lambda row: str(row.get("effective_ts_utc") or row.get("observed_at_utc") or ""), default=None)


def episode_markers(paths: list[Path], cache: LedgerCache, symbol: str) -> list[dict[str, Any]]:
    markers = []
    for path in paths:
        for row in cache.read(path):
            instrument = str(row.get("instrument") or row.get("symbol") or "SPY").upper()
            anchor = row.get("anchor_t0_utc")
            if instrument != symbol or not anchor or not row.get("counts_toward_n"):
                continue
            markers.append({"anchor_t0_utc": anchor, "arm": row.get("arm") or row.get("study_id"),
                            "cohort": row.get("cohort"), "record_hash": row.get("record_hash"),
                            "is_inferential": False})
    return sorted(markers, key=lambda marker: marker["anchor_t0_utc"])


class AlpacaBarSource:
    def __init__(self, api_key: str, api_secret: str) -> None:
        from alpaca.data.historical import StockHistoricalDataClient
        self.client = StockHistoricalDataClient(api_key, api_secret)

    def fetch(self, symbol: str, timeframe: str, now: datetime) -> list[dict[str, Any]]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        local = now.astimezone(ET)
        start = datetime.combine(local.date(), time(4, 0), tzinfo=ET).astimezone(timezone.utc)
        frame = TimeFrame.Minute if timeframe == "1m" else TimeFrame(5, TimeFrameUnit.Minute)
        response = self.client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=frame, start=start, end=now,
            feed=DataFeed.SIP, limit=1000,
        ))
        bars = getattr(response, "data", {}).get(symbol, [])
        return [{"ts": bar.timestamp.astimezone(timezone.utc).isoformat(),
                 "open": float(bar.open), "high": float(bar.high), "low": float(bar.low),
                 "close": float(bar.close), "volume": float(bar.volume)} for bar in bars]


class MarketStructureSource:
    def __init__(self, bars: Any, *, structure_ledger: Path = DEFAULT_STRUCTURE_LEDGER,
                 episode_ledgers: list[Path] | None = None, cache: LedgerCache | None = None) -> None:
        self.bars = bars
        self.structure_ledger = structure_ledger
        self.episode_ledgers = episode_ledgers or [DEFAULT_MULTI_EPISODES, DEFAULT_SPY_A, DEFAULT_SPY_B]
        self.cache = cache or LedgerCache()

    def fetch(self, symbol: str, timeframe: str, now: datetime) -> dict[str, Any]:
        if symbol not in SYMBOLS or timeframe not in TIMEFRAMES:
            return unavailable("INVALID_SELECTION", symbol, timeframe)
        try:
            candles = self.bars.fetch(symbol, timeframe, now)
        except Exception as exc:
            return unavailable(f"ALPACA_{type(exc).__name__}", symbol, timeframe)
        structure = latest_structure(self.cache.read(self.structure_ledger), symbol)
        if not candles and structure is None:
            return unavailable("CAPTURE_NOT_STARTED", symbol, timeframe, state="NOT_STARTED")
        bar_ts = parse_timestamp(candles[-1].get("ts")) if candles else None
        structure_ts = parse_timestamp((structure or {}).get("effective_ts_utc") or
                                       (structure or {}).get("observed_at_utc"))
        bar_age = (now - bar_ts).total_seconds() if bar_ts else None
        structure_age = (now - structure_ts).total_seconds() if structure_ts else None
        fresh = (bar_age is not None and bar_age <= BAR_MAX_AGE_SECONDS[timeframe]
                 and structure_age is not None and structure_age <= STRUCTURE_MAX_AGE_SECONDS
                 and (structure or {}).get("freshness", {}).get("status") == "FRESH")
        values = (structure or {}).get("values") or {}
        required = all(values.get(key) is not None for key in ("spot", "gamma_flip", "call_wall", "put_wall", "gamma_regime"))
        state = "LIVE" if fresh and required else "STALE"
        return {"status": state, "symbol": symbol, "timeframe": timeframe, "candles": candles,
                "structure": {key: values.get(key) for key in ("spot", "gamma_flip", "call_wall", "put_wall", "gamma_regime")},
                "structure_record_hash": (structure or {}).get("record_hash"),
                "markers": episode_markers(self.episode_ledgers, self.cache, symbol),
                "freshness": {"bar_age_seconds": bar_age, "structure_age_seconds": structure_age,
                              "bars": "FRESH" if bar_age is not None and bar_age <= BAR_MAX_AGE_SECONDS[timeframe] else "STALE_OR_MISSING",
                              "structure": "FRESH" if structure_age is not None and structure_age <= STRUCTURE_MAX_AGE_SECONDS else "STALE_OR_MISSING"},
                "label": LABEL, "is_inferential": False, "execution_authority": False,
                "admission_authority": False, "guard_authority": False}


def unavailable(reason: str, symbol: str, timeframe: str, *, state: str = "STALE") -> dict[str, Any]:
    return {"status": state, "reason": reason, "symbol": symbol, "timeframe": timeframe,
            "candles": [], "structure": None, "markers": [], "label": LABEL,
            "is_inferential": False, "execution_authority": False,
            "admission_authority": False, "guard_authority": False}


def make_handler(source: MarketStructureSource, echarts_path: Path) -> type[BaseHTTPRequestHandler]:
    class ReadOnlyChartHandler(BaseHTTPRequestHandler):
        def send_body(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "public, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers(); self.wfile.write(body)

        def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_body(status, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/static/echarts.min.js":
                try: body = echarts_path.read_bytes()
                except OSError: self.send_json({"error": "asset_unavailable"}, HTTPStatus.NOT_FOUND); return
                self.send_body(HTTPStatus.OK, body, "text/javascript; charset=utf-8"); return
            if parsed.path == "/health":
                self.send_json({"status": "READ_ONLY", "execution_authority": False}); return
            if parsed.path != "/chart":
                self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND); return
            query = parse_qs(parsed.query)
            symbol = str((query.get("symbol") or ["SPY"])[0]).upper()
            timeframe = str((query.get("timeframe") or ["1m"])[0]).lower()
            self.send_json(source.fetch(symbol, timeframe, datetime.now(timezone.utc)))

        def do_POST(self) -> None:  # noqa: N802
            self.send_json({"error": "read_only"}, HTTPStatus.METHOD_NOT_ALLOWED)
        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST
        def log_message(self, format: str, *args: object) -> None: return
    return ReadOnlyChartHandler


def source_from_environment(args: argparse.Namespace) -> MarketStructureSource:
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Alpaca Keychain environment unavailable")
    return MarketStructureSource(AlpacaBarSource(key, secret), structure_ledger=args.structure_ledger,
                                 episode_ledgers=[args.multi_episodes, args.spy_a, args.spy_b])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve",)); parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--structure-ledger", type=Path, default=DEFAULT_STRUCTURE_LEDGER)
    parser.add_argument("--multi-episodes", type=Path, default=DEFAULT_MULTI_EPISODES)
    parser.add_argument("--spy-a", type=Path, default=DEFAULT_SPY_A); parser.add_argument("--spy-b", type=Path, default=DEFAULT_SPY_B)
    parser.add_argument("--echarts", type=Path, default=DEFAULT_ECHARTS); args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(source_from_environment(args), args.echarts))
    print(json.dumps({"listen": f"http://{args.host}:{args.port}", "mode": "READ_ONLY", "execution_authority": False}), flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
