"""Append-only local evidence store for Scalpr V2 options intelligence."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from options_intelligence import canonical_hash, utc_now


DEFAULT_ROOT = Path("v2_data/options")


class OptionsFeatureStore:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = Path(root)
        self.raw_dir = self.root / "raw"
        self.snapshot_dir = self.root / "snapshots"
        self.feature_dir = self.root / "features"
        self.manifest_path = self.root / "manifest.jsonl"
        self._lock = threading.Lock()
        for path in (self.raw_dir, self.snapshot_dir, self.feature_dir):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _write_once(path: Path, value: dict) -> bool:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True

    def persist(self, *, raw_capture: dict, snapshot: dict, features: dict) -> dict:
        """Persist immutable raw, normalized, and derived artifacts by hash."""
        raw_id = canonical_hash(raw_capture)
        snapshot_id = snapshot.get("snapshot_id") or canonical_hash(snapshot)
        feature_id = features.get("feature_id") or canonical_hash(features)
        entries = {
            "raw": (self.raw_dir / f"{raw_id}.json", raw_capture),
            "snapshot": (self.snapshot_dir / f"{snapshot_id}.json", snapshot),
            "features": (self.feature_dir / f"{feature_id}.json", features),
        }
        with self._lock:
            created = {name: self._write_once(path, value)
                       for name, (path, value) in entries.items()}
            if any(created.values()):
                manifest = {
                    "recorded_at": utc_now(), "raw_id": raw_id,
                    "snapshot_id": snapshot_id, "feature_id": feature_id,
                    "created": created,
                }
                with self.manifest_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
                    handle.write("\n")
        return {
            "raw_id": raw_id, "snapshot_id": snapshot_id,
            "feature_id": feature_id, "created": created,
        }

    def persist_raw(self, raw_capture: dict) -> dict:
        """Record an incomplete/PENDING provider response without promoting it."""
        raw_id = canonical_hash(raw_capture)
        path = self.raw_dir / f"{raw_id}.json"
        with self._lock:
            created = self._write_once(path, raw_capture)
            if created:
                manifest = {
                    "recorded_at": utc_now(), "raw_id": raw_id,
                    "record_type": "raw_capture", "created": True,
                }
                with self.manifest_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
                    handle.write("\n")
        return {"raw_id": raw_id, "created": created}

    def status(self) -> dict:
        def count(directory: Path) -> int:
            return sum(1 for _ in directory.glob("*.json"))
        return {
            "available": True,
            "root": str(self.root),
            "raw_captures": count(self.raw_dir),
            "snapshots": count(self.snapshot_dir),
            "feature_records": count(self.feature_dir),
            "append_only": True,
        }
