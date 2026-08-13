"""Async, read-only Unusual Whales institutional-flow adapter."""

from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from institutional_flow import normalize_unusual_whales_alert


BASE_URL = "https://api.unusualwhales.com/api"
ENV_KEY = "UW_API_KEY"
ADAPTER_VERSION = "unusual-whales-flow-adapter-v0"
FLOW_ALERT_LIMIT = 200


class UnusualWhalesError(RuntimeError):
    pass


class UnusualWhalesRateLimited(UnusualWhalesError):
    pass


class UnusualWhalesAdapter:
    def __init__(self, token: Optional[str] = None, *, session=None,
                 timeout_seconds: float = 3.0, max_retries: int = 2,
                 dedupe_ttl_seconds: int = 3600, clock=None):
        self._token = (token or "").strip()
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.dedupe_ttl_seconds = int(dedupe_ttl_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._seen = OrderedDict()
        self._health = {
            "provider": "unusual_whales", "adapter_version": ADAPTER_VERSION,
            "configured": bool(self._token), "status": "NOT_CHECKED",
            "last_latency_ms": None, "last_error": None,
            "rate_limit_events": 0, "events_received": 0,
            "events_rejected": 0, "events_deduplicated": 0,
        }
        if self._token:
            self.session.headers.update({
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            })

    @classmethod
    def from_environment(cls, **kwargs):
        return cls(os.getenv(ENV_KEY), **kwargs)

    def status(self):
        return dict(self._health, mode="read_only", execution_authority=False)

    def _request_sync(self, path: str, params=None):
        if not self._token:
            raise UnusualWhalesError(f"Unusual Whales is unavailable because {ENV_KEY} is not configured")
        started = time.monotonic()
        last_status = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    f"{BASE_URL}/{path.lstrip('/')}", params=params,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    self._health.update(status="UNAVAILABLE", last_error="request_failed")
                    raise UnusualWhalesError("Unusual Whales request failed") from exc
                continue
            last_status = response.status_code
            if response.status_code == 429:
                self._health["rate_limit_events"] += 1
                if attempt >= self.max_retries:
                    self._health.update(status="RATE_LIMITED", last_error="http_429")
                    raise UnusualWhalesRateLimited("Unusual Whales API HTTP 429")
                time.sleep(min(0.25 * (2 ** attempt), 1.0))
                continue
            if response.status_code >= 500 and attempt < self.max_retries:
                continue
            if response.status_code != 200:
                self._health.update(status="UNAVAILABLE", last_error=f"http_{response.status_code}")
                raise UnusualWhalesError(f"Unusual Whales API HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise UnusualWhalesError("Unusual Whales API returned invalid JSON") from exc
            latency = (time.monotonic() - started) * 1000
            self._health.update(status="AVAILABLE", last_latency_ms=latency, last_error=None)
            return payload, latency
        raise UnusualWhalesError(f"Unusual Whales API HTTP {last_status}")

    async def _request(self, path: str, params=None):
        return await asyncio.to_thread(self._request_sync, path, params)

    def _dedupe(self, event) -> bool:
        now = self.clock().timestamp()
        cutoff = now - self.dedupe_ttl_seconds
        while self._seen and next(iter(self._seen.values())) < cutoff:
            self._seen.popitem(last=False)
        if event.dedupe_key in self._seen:
            self._health["events_deduplicated"] += 1
            return False
        self._seen[event.dedupe_key] = now
        return True

    async def fetch_recent_events(self, ticker: str, lookback_minutes: int):
        symbol = str(ticker).strip().upper()
        if not symbol or lookback_minutes <= 0:
            raise ValueError("ticker and a positive lookback_minutes are required")
        received_at = self.clock()
        payload, latency = await self._request(
            f"stock/{symbol}/flow-alerts", {"limit": FLOW_ALERT_LIMIT}
        )
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise UnusualWhalesError("Unusual Whales flow response was not a list")
        cutoff = received_at - timedelta(minutes=lookback_minutes)
        events = []
        self._health["events_received"] += len(rows)
        for row in rows:
            if not isinstance(row, dict):
                self._health["events_rejected"] += 1
                continue
            try:
                event = normalize_unusual_whales_alert(
                    row, received_at=received_at, provider_latency_ms=latency
                )
            except (TypeError, ValueError):
                self._health["events_rejected"] += 1
                continue
            if event.observed_at < cutoff or event.ticker != symbol:
                continue
            if self._dedupe(event):
                events.append(event)
        return events

    async def stream_events(self, tickers: list[str]):
        """Bounded REST polling stream; Kafka/WebSocket requires separate entitlement."""
        clean = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
        while True:
            for ticker in clean:
                for event in await self.fetch_recent_events(ticker, 1):
                    yield event
            await asyncio.sleep(5)

    async def health_check(self) -> bool:
        try:
            await self._request("stock/SPY/stock-state")
            return True
        except UnusualWhalesError:
            return False
