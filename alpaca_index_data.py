"""Read-only Alpaca index-value adapter for Wave shadow simulations.

Alpaca added native index values in June 2026, but alpaca-py 0.43.x does not
yet expose a Python client for them. This small adapter implements only the two
documented GET endpoints. It has no trading client and no order methods.

Historical SPX values arrive as timestamp/value points, not OHLCV bars. The
adapter caches the regular-session points incrementally and aggregates them to
one-minute high/low/close bars for Wave's existing 5-minute ATR calculation.
The `volume` field is a sample count and must never be described as exchange
volume; Wave labels its alignment reference as sampled TWAP for index studies.
"""

from __future__ import annotations

from datetime import datetime, timezone
import threading
import time

import requests


BASE_URL = "https://data.alpaca.markets"
LATEST_PATH = "/v1beta1/indices/latest/values"
VALUES_PATH = "/v1beta1/indices/values"
SOURCE_VERSION = "alpaca-native-index-values-v1"


class IndexDataError(RuntimeError):
    pass


def _parse_ts(value: str) -> datetime:
    if not value:
        raise IndexDataError("Alpaca index value is missing its provider timestamp")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise IndexDataError("Alpaca returned an invalid index timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class AlpacaIndexDataClient:
    """Credentialed, read-only client with a bounded incremental session cache."""

    def __init__(self, api_key: str, secret_key: str, *, session=None,
                 base_url: str = BASE_URL, timeout: float = 5.0,
                 refresh_seconds: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.refresh_seconds = refresh_seconds
        self.session = session or requests.Session()
        self.session.headers.update({
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
            "User-Agent": "Scalpr-Wave-Index/1.0",
        })
        self._lock = threading.Lock()
        self._session_cache = {}

    def _get(self, path, params):
        try:
            response = self.session.get(
                self.base_url + path, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise IndexDataError(f"Alpaca index request failed: {exc}") from exc
        if response.status_code != 200:
            try:
                detail = response.json().get("message") or response.text
            except Exception:
                detail = response.text
            detail = str(detail or "request rejected").strip().replace("\n", " ")[:180]
            raise IndexDataError(
                f"Alpaca index API HTTP {response.status_code}: {detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise IndexDataError("Alpaca index API returned invalid JSON") from exc

    def latest_value(self, symbol: str) -> dict:
        symbol = str(symbol).strip().upper()
        payload = self._get(LATEST_PATH, {"symbols": symbol})
        row = (payload.get("values") or {}).get(symbol)
        if not isinstance(row, dict) or row.get("v") is None:
            raise IndexDataError(f"Alpaca returned no latest value for {symbol}")
        ts = _parse_ts(row.get("t"))
        value = float(row["v"])
        if value <= 0:
            raise IndexDataError(f"Alpaca returned a non-positive value for {symbol}")
        return {
            "value": value,
            "provider_ts_epoch": ts.timestamp(),
            "provider_ts_iso": ts.isoformat(),
            "source": SOURCE_VERSION,
        }

    def historical_values(self, symbol: str, *, start: datetime, end: datetime,
                          max_pages: int = 10) -> list[dict]:
        """Fetch timestamp/value points with documented pagination."""
        symbol = str(symbol).strip().upper()
        params = {
            "symbols": symbol,
            "start": start.astimezone(timezone.utc).isoformat(),
            "end": end.astimezone(timezone.utc).isoformat(),
            "limit": 10000,
            "sort": "asc",
        }
        rows = []
        token = None
        for _ in range(max_pages):
            if token:
                params["page_token"] = token
            payload = self._get(VALUES_PATH, params)
            for row in (payload.get("values") or {}).get(symbol, []):
                if not isinstance(row, dict) or row.get("v") is None:
                    continue
                ts = _parse_ts(row.get("t"))
                value = float(row["v"])
                if value > 0:
                    rows.append({"ts": ts, "value": value})
            token = payload.get("next_page_token")
            if not token:
                break
        else:
            raise IndexDataError(
                f"Alpaca index pagination exceeded {max_pages} pages for {symbol}")
        return rows

    def minute_bars(self, symbol: str, *, session_open: datetime,
                    now: datetime) -> list[dict]:
        """Return regular-session minute bars, updating cached points at most
        once per refresh window. Output uses minutes-from-open in `t`."""
        symbol = str(symbol).strip().upper()
        open_utc = session_open.astimezone(timezone.utc)
        end_utc = now.astimezone(timezone.utc)
        cache_key = (symbol, open_utc.date().isoformat())
        monotonic_now = time.monotonic()
        with self._lock:
            state = self._session_cache.setdefault(
                cache_key, {"points": {}, "last_fetch": 0.0})
            if monotonic_now - state["last_fetch"] >= self.refresh_seconds:
                points = state["points"]
                fetch_start = open_utc
                if points:
                    fetch_start = max(points.values(), key=lambda row: row["ts"])["ts"]
                fresh = self.historical_values(
                    symbol, start=fetch_start, end=end_utc)
                for row in fresh:
                    if open_utc <= row["ts"] <= end_utc:
                        points[row["ts"].isoformat()] = row
                state["last_fetch"] = monotonic_now
            points = list(state["points"].values())

        buckets = {}
        for row in sorted(points, key=lambda item: item["ts"]):
            minute = int((row["ts"] - open_utc).total_seconds() // 60)
            if minute < 0:
                continue
            value = row["value"]
            bar = buckets.get(minute)
            if bar is None:
                buckets[minute] = {
                    "t": minute, "high": value, "low": value, "close": value,
                    "volume": 1.0, "sample_count": 1,
                }
            else:
                bar["high"] = max(bar["high"], value)
                bar["low"] = min(bar["low"], value)
                bar["close"] = value
                bar["volume"] += 1.0
                bar["sample_count"] += 1
        return [buckets[key] for key in sorted(buckets)]

