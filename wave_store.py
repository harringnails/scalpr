"""
Wave Riding durable state + audit storage, with restart recovery. SHADOW ONLY.

Reuses the Phase-1 durability primitives: the position record is written
whole-file atomically (temp + fsync + rename), and the append-only observation /
order / exit logs use `feature_engine._atomic_append` + tolerant reads. Order
dedupe is by idempotency key, so a restart mid-pending never double-fills.
"""
import json
import os
import tempfile

import feature_engine as fe
import wave_riding as wr

POSITION_DIR = "wave_positions"
OBSERVATIONS_LOG = "wave_observations_v0.jsonl"
ORDERS_LOG = "wave_orders_v0.jsonl"
EXITS_LOG = "wave_exits_v0.jsonl"
# observer-v0 additions
SEED_DIR = "wave_seeds"
ACTIVE_INDEX = "wave_active_positions_v0.json"
OBS_STREAM_DIR = "wave_obs_streams"


def _atomic_write_json(path, obj):
    """temp + fsync + rename — a crash mid-write can never leave a partial record
    at `path`; readers ignore the `.tmp`."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def position_path(position_id, base=POSITION_DIR):
    os.makedirs(base, exist_ok=True)
    safe = "".join(c for c in str(position_id) if c.isalnum() or c in "-_")
    return os.path.join(base, f"{safe}.json")


def save_position(pos, base=POSITION_DIR):
    _atomic_write_json(position_path(pos["position_id"], base), pos)
    return pos["position_id"]


def load_position(position_id, base=POSITION_DIR):
    p = position_path(position_id, base)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None                 # corrupt record → treat as absent (rebuild)


def append_observation(rec, path=OBSERVATIONS_LOG):
    fe._atomic_append(path, rec)


def order_seen(idempotency_key, path=ORDERS_LOG):
    for r in fe._iter_jsonl(path):
        if r.get("idempotency_key") == idempotency_key:
            return True
    return False


def append_order(rec, path=ORDERS_LOG):
    """Idempotent: an order with an already-recorded key is a no-op (returns
    False), so concurrent ticks / restart / replay never double-fill."""
    if order_seen(rec.get("idempotency_key"), path):
        return False
    fe._atomic_append(path, rec)
    return True


def append_exit(rec, path=EXITS_LOG):
    fe._atomic_append(path, rec)


# ── restart recovery ────────────────────────────────────────────────────────

def reconstruct(position_id, base=POSITION_DIR, orders_path=ORDERS_LOG):
    """Restore canonical state after a restart. Never regenerates signals:
      * load the persisted position record,
      * drop a pending order that never filled (no duplicate on restart),
      * require a FRESH confirmation interval before any new add,
      * leave qty/anchor/cost as last persisted (broker/sim reconciliation is
        a separate explicit step)."""
    pos = load_position(position_id, base)
    if pos is None:
        return None
    filled_keys = {r.get("idempotency_key") for r in fe._iter_jsonl(orders_path)
                   if r.get("filled")}
    if pos.get("pending_order_key") and pos["pending_order_key"] not in filled_keys:
        pos["pending_order_key"] = None
        if pos["state"] in (wr.ADD_PENDING, wr.ADD_CONFIRMING, wr.REVERSAL_TRIGGERED):
            pos["state"] = wr.MONITORING_WAVE
    pos["awaiting_fresh_confirmation"] = True     # engine resets confirm timer
    pos["confirm_started_at"] = None
    return pos


def reconcile_quantity(pos, external_open_qty):
    """If an external (broker/manual) quantity disagrees with our record, suspend
    and require an explicit rearm (§25). In shadow the external qty is the
    simulated qty, so this normally agrees."""
    if external_open_qty is not None and external_open_qty != pos.get("open_quantity"):
        p = dict(pos)
        p["state"] = wr.SUSPENDED
        p["reconcile_mismatch"] = {"record_qty": pos.get("open_quantity"),
                                   "external_qty": external_open_qty}
        return p, False
    return pos, True


# ── observer-v0: immutable seed, active index, per-position observation stream ─

def _safe(pid):
    return "".join(c for c in str(pid) if c.isalnum() or c in "-_")


def seed_path(pid, base=SEED_DIR):
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{_safe(pid)}.seed.json")


def save_seed(seed, base=SEED_DIR):
    """Persist the immutable shared seed BEFORE the simulation is marked active."""
    _atomic_write_json(seed_path(seed["shadow_position_id"], base), seed)
    return seed.get("seed_hash")


def load_seed(pid, base=SEED_DIR):
    p = seed_path(pid, base)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _load_index(path=ACTIVE_INDEX):
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []


def register_active(pid, path=ACTIVE_INDEX):
    idx = set(_load_index(path))
    idx.add(pid)
    _atomic_write_json(path, sorted(idx))


def unregister_active(pid, path=ACTIVE_INDEX):
    idx = [x for x in _load_index(path) if x != pid]
    _atomic_write_json(path, idx)


def list_active(path=ACTIVE_INDEX):
    return _load_index(path)


def obs_stream_path(pid, base=OBS_STREAM_DIR):
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{_safe(pid)}.obs.jsonl")


def append_obs_stream(pid, obs, base=OBS_STREAM_DIR):
    """Append one RAW observation to the per-position stream (used for the
    finalize-time three-track replay). Crash-safe append + tolerant read."""
    return fe._atomic_append(obs_stream_path(pid, base), obs)


def read_obs_stream(pid, base=OBS_STREAM_DIR):
    return list(fe._iter_jsonl(obs_stream_path(pid, base)))


def last_processed_sequence(pid, base=OBS_STREAM_DIR):
    """Highest canonical observation sequence already applied (or -1)."""
    seqs = [o.get("observation_sequence", -1) for o in read_obs_stream(pid, base)]
    return max(seqs) if seqs else -1


def observation_id_seen(pid, observation_id, base=OBS_STREAM_DIR):
    for o in read_obs_stream(pid, base):
        if o.get("observation_id") == observation_id:
            return True
    return False
