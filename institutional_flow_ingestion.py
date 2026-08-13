"""One-shot institutional-flow ingestion; never runs unless called explicitly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from institutional_flow import aggregate_flow_events
from unusual_whales_adapter import UnusualWhalesError


class InstitutionalFlowIngestionService:
    def __init__(self, adapter, store, *, windows=(1, 5, 15, 30), clock=None):
        self.adapter = adapter
        self.store = store
        self.windows = tuple(int(value) for value in windows)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def run_once(self, *, ticker: str, lookback_minutes: int = 30) -> dict:
        now = self.clock()
        try:
            events = await self.adapter.fetch_recent_events(ticker, lookback_minutes)
        except UnusualWhalesError as exc:
            status = self.adapter.status()
            self.store.record_health(status)
            self.store.record_error("unusual_whales", status.get("last_error") or "provider_unavailable")
            snapshots = []
            for window in self.windows:
                snapshot = aggregate_flow_events(
                    [], ticker=ticker, window_minutes=window, window_end=now,
                    provider_available=False,
                )
                self.store.append_snapshot(snapshot)
                snapshots.append(snapshot)
            return {
                "provider_status": "UNAVAILABLE", "events_received": 0,
                "events_persisted": 0, "snapshots": len(snapshots),
                "error": type(exc).__name__, "execution_authority": False,
            }

        inserted = sum(self.store.append_event(event) for event in events)
        history = self.store.events_between(
            ticker=ticker,
            start=now - timedelta(minutes=max(self.windows)),
            end=now,
        )
        snapshots = []
        for window in self.windows:
            snapshot = aggregate_flow_events(
                history, ticker=ticker, window_minutes=window, window_end=now,
                provider_available=True,
            )
            self.store.append_snapshot(snapshot)
            snapshots.append(snapshot)
        self.store.record_health(self.adapter.status())
        return {
            "provider_status": "AVAILABLE", "events_received": len(events),
            "events_persisted": inserted, "snapshots": len(snapshots),
            "execution_authority": False,
        }
