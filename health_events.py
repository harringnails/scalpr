"""Bounded runtime-health event stream; never participates in trading decisions."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


HEALTH_VERSION = "scalpr-runtime-health-v1"
HEALTH_LOG = Path("runtime_health_v1.jsonl")


class HealthEventLog:
    """Write transitions immediately and unchanged heartbeats at a bounded cadence."""

    def __init__(self, path=HEALTH_LOG, heartbeat_seconds=300, clock=time.time):
        self.path = Path(path)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.clock = clock
        self._states = {}
        self._lock = threading.Lock()

    def record(self, component, status, *, detail=None, fields=None, force=False):
        now = self.clock()
        key = str(component)
        status = str(status)
        with self._lock:
            prior = self._states.get(key)
            changed = prior is None or prior[0] != status or prior[1] != detail
            due = prior is None or now - prior[2] >= self.heartbeat_seconds
            if not (force or changed or due):
                return None
            rec = {
                "health_version": HEALTH_VERSION,
                "ts": datetime.now(timezone.utc).isoformat(),
                "component": key,
                "status": status,
                "detail": detail,
                "fields": fields or {},
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                body = (json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n").encode()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    os.write(fd, body)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                return None  # observability must never affect Guard/order behavior
            self._states[key] = (status, detail, now)
            return rec
