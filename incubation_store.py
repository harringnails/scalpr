"""
ENTRY_INCUBATION_SHADOW persistence — append-only, restart-safe, idempotent.
Reuses feature_engine atomics. Stores ONE canonical option path per trade plus
the immutable entry snapshot. No live behavior.
"""
import json
import os
import tempfile
from datetime import datetime, timezone

import feature_engine as fe

SNAPSHOT_DIR = "incubation_snapshots"
PATH_STREAM_DIR = "incubation_paths"
TRADE_DIR = "incubation_trades"
ACTIVE_INDEX = "incubation_active_v0.json"


def _safe(x):
    return "".join(c for c in str(x) if c.isalnum() or c in "-_")


def _atomic_write_json(path, obj):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except OSError: pass


def snapshot_path(tid): os.makedirs(SNAPSHOT_DIR, exist_ok=True); return os.path.join(SNAPSHOT_DIR, f"{_safe(tid)}.json")
def trade_path(tid): os.makedirs(TRADE_DIR, exist_ok=True); return os.path.join(TRADE_DIR, f"{_safe(tid)}.json")
def stream_path(tid): os.makedirs(PATH_STREAM_DIR, exist_ok=True); return os.path.join(PATH_STREAM_DIR, f"{_safe(tid)}.jsonl")


def save_snapshot(snap):
    """Immutable — written once at entry; never overwritten."""
    p = snapshot_path(snap["trade_id"])
    if os.path.exists(p):
        return None
    _atomic_write_json(p, snap)
    return snap.get("snapshot_hash")


def load_snapshot(tid):
    p = snapshot_path(tid)
    if not os.path.exists(p): return None
    try:
        with open(p) as f: return json.load(f)
    except Exception: return None


def save_trade(rec): _atomic_write_json(trade_path(rec["trade_id"]), rec)
def load_trade(tid):
    p = trade_path(tid)
    if not os.path.exists(p): return None
    try:
        with open(p) as f: return json.load(f)
    except Exception: return None


def append_observation(tid, obs): return fe._atomic_append(stream_path(tid), obs)
def read_path(tid): return list(fe._iter_jsonl(stream_path(tid)))


def last_option_quote_ts(tid):
    ts = [o.get("option_quote_timestamp") for o in read_path(tid)]
    ts = [t for t in ts if t]
    return ts[-1] if ts else None


def last_sequence(tid):
    seqs = [o.get("observation_sequence", -1) for o in read_path(tid)]
    return max(seqs) if seqs else -1


# active index
def _load_index():
    if not os.path.exists(ACTIVE_INDEX): return []
    try:
        with open(ACTIVE_INDEX) as f: return json.load(f)
    except Exception: return []

def register_active(tid):
    idx = set(_load_index()); idx.add(tid); _atomic_write_json(ACTIVE_INDEX, sorted(idx))

def unregister_active(tid):
    _atomic_write_json(ACTIVE_INDEX, [x for x in _load_index() if x != tid])

def list_active(): return _load_index()


# ── validation gate: Cohort A counting starts only after one clean validation ─
VALIDATION_STATE = "incubation_validation_state.json"

def validation_passed():
    if not os.path.exists(VALIDATION_STATE): return False
    try:
        with open(VALIDATION_STATE) as f: return bool(json.load(f).get("passed"))
    except Exception: return False

def set_validation_passed(passed, trade_id=None):
    _atomic_write_json(VALIDATION_STATE, {"passed": bool(passed), "validated_by": trade_id,
                                          "at": datetime.now(timezone.utc).isoformat()})


# ── operational telemetry (append-only per trade) ───────────────────────────
def telemetry_path(tid): os.makedirs("incubation_telemetry", exist_ok=True); return os.path.join("incubation_telemetry", f"{_safe(tid)}.jsonl")
def append_telemetry(tid, event): return fe._atomic_append(telemetry_path(tid), event)
def read_telemetry(tid): return list(fe._iter_jsonl(telemetry_path(tid)))
