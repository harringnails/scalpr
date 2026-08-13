"""Lossless rotation and cheap storage-health inspection for local evidence logs."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


ROTATION_VERSION = "scalpr-log-rotation-v1"
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
MANIFEST_NAME = "rotation_manifest_v1.jsonl"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rotate_csv(path, archive_dir=None, max_bytes=DEFAULT_MAX_BYTES, now=None):
    """Move a complete active CSV into a gzip archive and retain its header.

    The archive is hashed and recorded before the uncompressed source is
    replaced. Historical rows are never rewritten or deleted.
    """
    source = Path(path)
    if not source.exists() or source.stat().st_size < int(max_bytes):
        return None
    archive_root = Path(archive_dir or source.parent / "tick_logs")
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    destination = archive_root / f"{source.stem}_{stamp}.csv.gz"
    temp = destination.with_suffix(destination.suffix + ".tmp")
    with source.open("rb") as src, gzip.open(temp, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    os.replace(temp, destination)
    archive_hash = sha256_file(destination)

    with source.open("r", newline="") as handle:
        header = handle.readline()
    replacement = source.with_suffix(source.suffix + ".new")
    with replacement.open("w", newline="") as handle:
        handle.write(header)
        handle.flush()
        os.fsync(handle.fileno())
    source_bytes = source.stat().st_size
    os.replace(replacement, source)

    rec = {
        "rotation_version": ROTATION_VERSION,
        "rotated_at": datetime.now(timezone.utc).isoformat(),
        "source": source.name,
        "source_bytes": source_bytes,
        "archive": destination.name,
        "archive_bytes": destination.stat().st_size,
        "archive_sha256": archive_hash,
        "active_header_retained": bool(header),
    }
    manifest = archive_root / MANIFEST_NAME
    with manifest.open("a") as handle:
        handle.write(json.dumps(rec, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return rec


def storage_health(root=".", tracked=None):
    base = Path(root)
    usage = shutil.disk_usage(base)
    paths = tracked or [
        "tick_log.csv", "scalp_journal.csv", "runtime_health_v1.jsonl",
        "incubation_paths", "incubation_telemetry", "wave_obs_streams",
    ]
    sizes = {}
    for relative in paths:
        path = base / relative
        if path.is_file():
            sizes[relative] = path.stat().st_size
        elif path.is_dir():
            sizes[relative] = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        else:
            sizes[relative] = 0
    free_ratio = usage.free / usage.total if usage.total else 0.0
    return {
        "rotation_version": ROTATION_VERSION,
        "disk_total_bytes": usage.total,
        "disk_free_bytes": usage.free,
        "disk_free_percent": round(free_ratio * 100, 2),
        "status": "CRITICAL" if free_ratio < 0.05 else "WARN" if free_ratio < 0.10 else "OK",
        "tracked_bytes": sizes,
    }
