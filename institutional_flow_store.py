"""Local indexed store for normalized flow evidence and provider health."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from institutional_flow import InstitutionalFlowEvent


DEFAULT_PATH = Path("institutional_flow_v0.db")


class InstitutionalFlowStore:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS institutional_flow_events (
                    dedupe_key TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    event_id TEXT NOT NULL, ticker TEXT NOT NULL,
                    observed_at TEXT NOT NULL, received_at TEXT NOT NULL,
                    option_symbol TEXT, expiration TEXT, strike TEXT,
                    option_type TEXT, raw_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_flow_ticker_observed
                    ON institutional_flow_events(ticker, observed_at);
                CREATE INDEX IF NOT EXISTS idx_flow_option_symbol
                    ON institutional_flow_events(option_symbol);
                CREATE TABLE IF NOT EXISTS institutional_flow_snapshots (
                    snapshot_key TEXT PRIMARY KEY, ticker TEXT NOT NULL,
                    window_end TEXT NOT NULL, window_minutes INTEGER NOT NULL,
                    raw_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_flow_snapshots_ticker_end
                    ON institutional_flow_snapshots(ticker, window_end);
                CREATE TABLE IF NOT EXISTS institutional_flow_provider_health (
                    recorded_at TEXT NOT NULL, provider TEXT NOT NULL,
                    status TEXT NOT NULL, raw_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS institutional_flow_ingestion_errors (
                    recorded_at TEXT NOT NULL, provider TEXT NOT NULL,
                    error_code TEXT NOT NULL, detail TEXT
                );
            """)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _json(model):
        return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def append_event(self, event) -> bool:
        raw = self._json(event)
        with self._connect() as conn:
            return bool(conn.execute("""
                INSERT OR IGNORE INTO institutional_flow_events
                (dedupe_key, provider, event_id, ticker, observed_at, received_at,
                 option_symbol, expiration, strike, option_type, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (event.dedupe_key, event.provider, event.event_id, event.ticker,
                  event.observed_at.isoformat(), event.received_at.isoformat(),
                  event.option_symbol,
                  event.expiration.isoformat() if event.expiration else None,
                  str(event.strike) if event.strike is not None else None,
                  event.option_type, raw)).rowcount)

    def events_between(self, *, ticker: str, start: datetime,
                       end: datetime) -> list[InstitutionalFlowEvent]:
        """Load the immutable normalized events for a deterministic UTC window."""
        symbol = str(ticker).strip().upper()
        if not symbol or start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError("ticker and an ordered timezone-aware window are required")
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT raw_json FROM institutional_flow_events
                WHERE ticker = ? AND observed_at >= ? AND observed_at <= ?
                ORDER BY observed_at, dedupe_key
            """, (symbol, start.astimezone(timezone.utc).isoformat(),
                  end.astimezone(timezone.utc).isoformat())).fetchall()
        return [InstitutionalFlowEvent.model_validate_json(row[0]) for row in rows]

    def append_snapshot(self, snapshot) -> bool:
        raw = self._json(snapshot)
        key = f"{snapshot.schema_version}:{snapshot.ticker}:{snapshot.window_minutes}:{snapshot.window_end.isoformat()}"
        with self._connect() as conn:
            return bool(conn.execute("""
                INSERT OR IGNORE INTO institutional_flow_snapshots
                (snapshot_key, ticker, window_end, window_minutes, raw_json)
                VALUES (?, ?, ?, ?, ?)
            """, (key, snapshot.ticker, snapshot.window_end.isoformat(),
                  snapshot.window_minutes, raw)).rowcount)

    def record_health(self, status: dict):
        clean = {k: v for k, v in status.items() if k not in {"token", "authorization", "headers"}}
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO institutional_flow_provider_health
                (recorded_at, provider, status, raw_json) VALUES (?, ?, ?, ?)
            """, (datetime.now(timezone.utc).isoformat(), clean.get("provider", "unknown"),
                  clean.get("status", "UNKNOWN"), json.dumps(clean, sort_keys=True)))

    def record_error(self, provider: str, error_code: str, detail=None):
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO institutional_flow_ingestion_errors
                (recorded_at, provider, error_code, detail) VALUES (?, ?, ?, ?)
            """, (datetime.now(timezone.utc).isoformat(), provider, error_code,
                  str(detail)[:180] if detail else None))

    def status(self):
        with self._connect() as conn:
            counts = {
                "events": conn.execute("SELECT COUNT(*) FROM institutional_flow_events").fetchone()[0],
                "snapshots": conn.execute("SELECT COUNT(*) FROM institutional_flow_snapshots").fetchone()[0],
                "health_records": conn.execute("SELECT COUNT(*) FROM institutional_flow_provider_health").fetchone()[0],
                "ingestion_errors": conn.execute("SELECT COUNT(*) FROM institutional_flow_ingestion_errors").fetchone()[0],
            }
        return {"available": True, "path": str(self.path), **counts}
