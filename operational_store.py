"""Local SQLite operational mirror for fast indexed dashboard reads.

CSV/JSONL files remain the evidence sources during migration. This database is
rebuildable, local, Git-ignored, and must never be allowed to affect an order or
Guard action when unavailable.
"""

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path("scalpr_state.db")


class OperationalStore:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    row_hash TEXT PRIMARY KEY,
                    source_row INTEGER NOT NULL,
                    utc_time TEXT,
                    mode TEXT,
                    symbol TEXT,
                    realized_pct REAL,
                    raw_json TEXT NOT NULL,
                    mirrored_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(utc_time);
                CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades(mode);
                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);

                CREATE TABLE IF NOT EXISTS guard_events (
                    event_hash TEXT PRIMARY KEY,
                    ts TEXT,
                    action TEXT,
                    symbol TEXT,
                    raw_json TEXT NOT NULL,
                    mirrored_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_guard_events_symbol_time
                    ON guard_events(symbol, ts);

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _canonical(record):
        return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _csv_record(record):
        # Match csv.DictReader semantics so a freshly mirrored row and the same
        # row re-read from scalp_journal.csv have one stable identity.
        return {str(key): ("" if value is None else str(value))
                for key, value in record.items()}

    @classmethod
    def _hash(cls, record):
        return hashlib.sha256(cls._canonical(record).encode("utf-8")).hexdigest()

    def _insert_trade(self, conn, record, source_row):
        record = self._csv_record(record)
        raw = self._canonical(record)
        return conn.execute("""
            INSERT OR IGNORE INTO trades
            (row_hash, source_row, utc_time, mode, symbol, realized_pct, raw_json, mirrored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (self._hash(record), int(source_row), record.get("utc_time"),
              record.get("mode", "paper"), record.get("symbol"),
              _float_or_none(record.get("realized_pct")), raw, _utc_now())).rowcount

    def sync_journal(self, csv_path):
        path = Path(csv_path)
        if not path.exists():
            return 0
        with path.open(newline="") as f:
            rows = [self._csv_record(row) for row in csv.DictReader(f)]
        inserted = 0
        with self._connect() as conn:
            # The CSV schema can grow (for example ADR-019 scope stamps). Row
            # hashes then legitimately change even though these are the same
            # source rows. This database is a rebuildable mirror, so compare it
            # to the authoritative CSV and rebuild atomically when it differs;
            # otherwise a schema migration would duplicate every old trade.
            mirrored = [json.loads(row["raw_json"]) for row in conn.execute(
                "SELECT raw_json FROM trades ORDER BY source_row, rowid")]
            if mirrored == rows:
                self._set_metadata(conn, "journal_source", str(path))
                self._set_metadata(conn, "journal_rows", str(len(rows)))
                return 0
            conn.execute("DELETE FROM trades")
            for source_row, record in enumerate(rows):
                inserted += self._insert_trade(conn, record, source_row)
            self._set_metadata(conn, "journal_source", str(path))
            self._set_metadata(conn, "journal_rows", str(len(rows)))
        return inserted

    def append_trade(self, record):
        with self._connect() as conn:
            source_row = conn.execute(
                "SELECT COALESCE(MAX(source_row), -1) + 1 FROM trades"
            ).fetchone()[0]
            return self._insert_trade(conn, record, source_row)

    def journal_rows(self, mode=None):
        sql = "SELECT raw_json FROM trades"
        args = ()
        if mode is not None:
            sql += " WHERE mode = ?"
            args = (mode,)
        sql += " ORDER BY source_row, rowid"
        with self._connect() as conn:
            return [json.loads(row["raw_json"]) for row in conn.execute(sql, args)]

    def record_guard_event(self, record):
        raw = self._canonical(record)
        with self._connect() as conn:
            return conn.execute("""
                INSERT OR IGNORE INTO guard_events
                (event_hash, ts, action, symbol, raw_json, mirrored_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (self._hash(record), record.get("ts"), record.get("action"),
                  record.get("symbol"), raw, _utc_now())).rowcount

    def _set_metadata(self, conn, key, value):
        conn.execute("""
            INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (key, value, _utc_now()))

    def status(self):
        with self._connect() as conn:
            trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM guard_events").fetchone()[0]
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        return {"available": True, "path": str(self.path), "journal_mode": mode,
                "trades": trades, "guard_events": events}


def _float_or_none(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _utc_now():
    return datetime.now(timezone.utc).isoformat()
