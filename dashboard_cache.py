"""Small thread-safe TTL cache for dashboard read models.

The cache deduplicates file parsing and broker reads across browser tabs. It is
strictly an acceleration layer: trading state and raw evidence remain owned by
their existing sources.
"""

import threading
import time


class DashboardCache:
    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._entries = {}
        self._lock = threading.Lock()
        self._key_locks = {}

    def _key_lock(self, key):
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def get(self, key, max_age=None):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            age = self._clock() - entry["stored_at"]
            if max_age is not None and age > max_age:
                return None
            return entry["value"]

    def get_or_compute(self, key, ttl_seconds, compute, stale_on_error=True):
        cached = self.get(key, ttl_seconds)
        if cached is not None:
            return cached
        key_lock = self._key_lock(key)
        with key_lock:
            cached = self.get(key, ttl_seconds)
            if cached is not None:
                return cached
            stale = self.get(key)
            try:
                value = compute()
            except Exception:
                if stale_on_error and stale is not None:
                    return stale
                raise
            with self._lock:
                self._entries[key] = {"value": value, "stored_at": self._clock()}
            return value

    def invalidate(self, *keys):
        with self._lock:
            if keys:
                for key in keys:
                    self._entries.pop(key, None)
            else:
                self._entries.clear()

    def metadata(self):
        now = self._clock()
        with self._lock:
            return {key: {"age_seconds": round(max(0.0, now - entry["stored_at"]), 3)}
                    for key, entry in self._entries.items()}
