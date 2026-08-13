#!/usr/bin/env python3
"""Incrementally mirror append-only Scalpr evidence into Cloud SQL.

This process has no broker imports and no execution authority. Local CSV/JSONL
files remain authoritative; PostgreSQL is an idempotent, queryable mirror.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from cloudsql_database import CloudSqlDatabase, configured


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "v2_data/cloudsql_mirror_state.json"
STATUS_PATH = ROOT / "v2_data/cloudsql_mirror_status.json"

RAW_STREAMS = (
    ("runtime_health_v1.jsonl", "scalpr", "runtime_health", "scalpr-runtime-health-v1"),
    ("entry_policy_prototype_v0_decisions.jsonl", "scalpr", "entry_decisions", "entry-policy-exploratory-v0"),
    ("entry_policy_prototype_v0_outcomes.jsonl", "scalpr", "entry_outcomes", "entry-policy-exploratory-v0"),
    ("directional_shadow_v0.jsonl", "scalpr", "directional_shadow", "directional-shadow-v0"),
    ("wave_observations_v0.jsonl", "scalpr", "wave_observations", "wave-riding-v0"),
    ("wave_exits_v0.jsonl", "scalpr", "wave_exits", "wave-riding-v0"),
    ("scalpr_intel_labels_lifecycle_v0.jsonl", "scalpr", "intel_labels", "label-lifecycle-v0"),
)
FEATURE_STREAM = "scalpr_intel_feature_snapshots_v0.jsonl"
JOURNAL = "scalp_journal.csv"

RAW_INSERT = text("""
    INSERT INTO scalpr_v2.raw_capture
    (content_hash, provider, dataset, symbol, schema_version,
     provider_timestamp, received_at, payload)
    VALUES (:content_hash, :provider, :dataset, :symbol, :schema_version,
            :provider_timestamp, :received_at, CAST(:payload AS JSONB))
    ON CONFLICT (content_hash) DO NOTHING
""")
FEATURE_INSERT = text("""
    INSERT INTO scalpr_v2.feature_snapshots
    (content_hash, source_hash, symbol, feature_version, observed_at,
     missing_inputs, features)
    VALUES (:content_hash, :source_hash, :symbol, :feature_version, :observed_at,
            CAST(:missing_inputs AS JSONB), CAST(:features AS JSONB))
    ON CONFLICT (content_hash) DO NOTHING
""")
JOURNAL_INSERT = text("""
    INSERT INTO scalpr_v2.trade_journal_mirror
    (row_hash, source_row, utc_time, mode, symbol, raw_record)
    VALUES (:row_hash, :source_row, :utc_time, :mode, :symbol,
            CAST(:raw_record AS JSONB))
    ON CONFLICT (row_hash) DO NOTHING
""")


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _timestamp(record: dict):
    for key in ("provider_ts", "observed_at", "decision_timestamp",
                "decision_time", "outcomes_recorded_at", "as_of",
                "signal_ts", "ts", "utc_time", "calculation_timestamp"):
        value = record.get(key)
        if value:
            return str(value)
    return None


def _symbol(record: dict):
    return (record.get("symbol") or record.get("ticker")
            or record.get("underlying"))


def _raw_row(record: dict, provider: str, dataset: str, schema_version: str):
    observed = _timestamp(record)
    received = observed or datetime.now(timezone.utc).isoformat()
    return {
        "content_hash": _hash({"provider": provider, "dataset": dataset,
                               "payload": record}),
        "provider": provider,
        "dataset": dataset,
        "symbol": _symbol(record),
        "schema_version": str(record.get("schema_version")
                              or record.get("version") or schema_version),
        "provider_timestamp": observed,
        "received_at": received,
        "payload": _canonical(record),
    }


def _collect_missing(value, prefix=""):
    missing = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if key in {"_unwired", "missing_inputs", "missing_fields"} and isinstance(child, list):
                missing.extend(f"{prefix}.{item}" if prefix else str(item) for item in child)
            else:
                missing.extend(_collect_missing(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            missing.extend(_collect_missing(child, f"{prefix}[{index}]"))
    return sorted(set(missing))


def _feature_row(record: dict):
    features = record.get("feature_record") or {}
    content_hash = record.get("feature_snapshot_hash") or _hash(features)
    return {
        "content_hash": content_hash,
        "source_hash": _hash(record),
        "symbol": str(record.get("ticker") or features.get("ticker") or "SPY"),
        "feature_version": str(record.get("schema_version")
                               or features.get("schema_version") or "scalpr-intel-v0"),
        "observed_at": (record.get("decision_timestamp")
                        or features.get("timestamp")),
        "missing_inputs": _canonical(_collect_missing(features)),
        "features": _canonical(features),
    }


def _read_jsonl_batch(path: Path, offset: int, limit: int):
    records, next_offset = [], int(offset or 0)
    if not path.exists():
        return records, next_offset
    size = path.stat().st_size
    if next_offset > size:  # rotation/replacement: hashes make replay idempotent
        next_offset = 0
    with path.open("rb") as handle:
        handle.seek(next_offset)
        while len(records) < limit:
            start = handle.tell()
            line = handle.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                handle.seek(start)
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL record in {path.name} at byte {start}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"non-object JSONL record in {path.name} at byte {start}")
            records.append(record)
            next_offset = handle.tell()
    return records, next_offset


def _load_json(path: Path, default):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, type(default)) else default
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _journal_rows(path: Path):
    if not path.exists():
        return []
    rows = []
    with path.open(newline="") as handle:
        for source_row, record in enumerate(csv.DictReader(handle), start=2):
            rows.append({
                "row_hash": _hash(record), "source_row": source_row,
                "utc_time": record.get("utc_time") or None,
                "mode": record.get("mode") or None,
                "symbol": record.get("symbol") or None,
                "raw_record": _canonical(record),
            })
    return rows


def sync_once(database: CloudSqlDatabase, *, max_records=2000) -> dict:
    state = _load_json(STATE_PATH, {"schema_version": "cloudsql-mirror-state-v1", "offsets": {}})
    offsets = state.setdefault("offsets", {})
    result = {"raw_records": 0, "feature_records": 0, "journal_rows": 0,
              "backlog_remaining": False}
    engine = database.engine()

    journal = _journal_rows(ROOT / JOURNAL)
    if journal:
        with engine.begin() as connection:
            connection.execute(JOURNAL_INSERT, journal)
        result["journal_rows"] = len(journal)

    for filename, provider, dataset, version in RAW_STREAMS:
        path = ROOT / filename
        old_offset = int(offsets.get(filename, 0) or 0)
        records, new_offset = _read_jsonl_batch(path, old_offset, max_records)
        if records:
            rows = [_raw_row(record, provider, dataset, version) for record in records]
            with engine.begin() as connection:
                connection.execute(RAW_INSERT, rows)
            offsets[filename] = new_offset
            result["raw_records"] += len(rows)
        if path.exists() and new_offset < path.stat().st_size:
            result["backlog_remaining"] = True

    feature_path = ROOT / FEATURE_STREAM
    old_offset = int(offsets.get(FEATURE_STREAM, 0) or 0)
    records, new_offset = _read_jsonl_batch(feature_path, old_offset, max_records)
    if records:
        rows = [_feature_row(record) for record in records]
        if any(not row["observed_at"] for row in rows):
            raise ValueError("feature snapshot is missing its observation timestamp")
        with engine.begin() as connection:
            connection.execute(FEATURE_INSERT, rows)
        offsets[FEATURE_STREAM] = new_offset
        result["feature_records"] = len(rows)
    if feature_path.exists() and new_offset < feature_path.stat().st_size:
        result["backlog_remaining"] = True

    state["last_success_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(STATE_PATH, state)
    status = {
        "status": "OK", "execution_authority": False,
        "local_files_authoritative": True, **result,
        "updated_at": state["last_success_at"],
    }
    _write_json(STATUS_PATH, status)
    return status


def _write_error_status(exc: Exception):
    _write_json(STATUS_PATH, {
        "status": "ERROR", "error_type": type(exc).__name__,
        "execution_authority": False, "local_files_authoritative": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mirror Scalpr evidence to Cloud SQL")
    parser.add_argument("--loop", action="store_true", help="continue at a bounded cadence")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--max-records", type=int, default=2000)
    args = parser.parse_args(argv)
    if not configured():
        print("Cloud SQL mirror is not configured.")
        return 1
    while True:
        try:
            with CloudSqlDatabase() as database:
                result = sync_once(database, max_records=max(1, args.max_records))
            print(json.dumps(result, sort_keys=True), flush=True)
        except Exception as exc:
            _write_error_status(exc)
            print(f"Cloud SQL mirror failed ({type(exc).__name__}).", flush=True)
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(max(15.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
