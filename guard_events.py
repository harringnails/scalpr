"""
guard_events.py — pause/resume (disengage/re-engage) event capture.

Records every guard disengage/re-engage that today only prints to the terminal,
so "did I disengage this trade, and did it re-engage or get closed manually?"
becomes answerable from real records instead of peak-value inference.

Read-only w.r.t. trading: it observes the guard's state at the moment of an
existing pause/resume action. It does NOT change the Guard, orders, or Standard
mode — the pause/resume behavior is identical with or without this log.
"""
from collections import Counter
from datetime import datetime, timezone

import feature_engine as fe

GUARD_EVENTS_LOG = "guard_events_v0.jsonl"
GUARD_EVENTS_VERSION = "guard-events-v0"


def log_event(action, guard, path=GUARD_EVENTS_LOG, actor=None, reason=None,
              request_id=None):
    """Append one disengage/re-engage event with the guard's state at that moment.
    `action` ∈ {"pause","resume"}. Never raises into the caller."""
    try:
        entry = getattr(guard, "entry", None)
        last = getattr(guard, "last", None)
        profit = ((last - entry) / entry * 100) if (entry and last) else None
        rec = {
            "events_version": GUARD_EVENTS_VERSION,
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "symbol": getattr(guard, "symbol", None),
            "kind": getattr(guard, "kind", None),
            "entry": entry, "last": last, "qty": getattr(guard, "qty", None),
            "profit_pct": (round(profit, 3) if profit is not None else None),
            "peak_pct": round(getattr(guard, "peak", 0.0) or 0.0, 3),
            "paused_after": getattr(guard, "paused", None),
            "actor": actor,
            "reason": reason,
            "request_id": request_id,
        }
        fe._atomic_append(path, rec)
        return rec
    except Exception:
        return None      # capture must never affect the live pause/resume action


def read_events(symbol=None, path=GUARD_EVENTS_LOG):
    evs = list(fe._iter_jsonl(path))
    if symbol:
        evs = [e for e in evs if e.get("symbol") == symbol.upper()]
    return evs


def summary(path=GUARD_EVENTS_LOG):
    evs = list(fe._iter_jsonl(path))
    acts = Counter(e.get("action") for e in evs)
    return {"events_version": GUARD_EVENTS_VERSION, "total_events": len(evs),
            "pause": acts.get("pause", 0), "resume": acts.get("resume", 0),
            "symbols_disengaged": sorted({e.get("symbol") for e in evs
                                          if e.get("action") == "pause" and e.get("symbol")}),
            "note": "Read-only capture of live disengage/re-engage; no behavior change."}
