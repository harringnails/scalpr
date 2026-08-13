"""
ENTRY_INCUBATION_SHADOW server integration helper — READ-ONLY.

Ready but DORMANT: not wired into scalp_server and the feature flag is OFF by
default, so nothing captures until (a) the cohort config is approved/locked and
(b) the flag is enabled. Never touches the live Guard, orders, or Standard mode.

On approval, the entire live wiring is two additive, gated, exception-isolated
touch points in scalp_server:
  * right after `self.guards[sym] = Guard(...)`:
        if incubation_config.enabled():
            try: incubation_server.on_live_entry(self, sym, guard)
            except Exception as e: log(...)
  * at the end of `_poll_loop`:
        try: incubation_server.observer_tick(self)
        except Exception as e: log(...)
Plus read-only GET endpoints for status / report / proposal.
"""
import re
import threading
import time
from datetime import datetime, timezone

import incubation_config as icfg
import incubation_shadow as ish
import incubation_store as ist
import incubation_cohort as icoh


def _parse_occ(sym):
    m = re.match(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", str(sym or ""))
    if not m:
        return None
    root, yy, mm, dd, cp, strike = m.groups()
    return {"root": root, "expiration": f"20{yy}-{mm}-{dd}", "type": cp,
            "strike": int(strike) / 1000.0, "direction": "CALL" if cp == "C" else "PUT"}


def _guard_fields(platform, sym, guard):
    occ = _parse_occ(sym) or {}
    dte = None
    try:
        exp = datetime.fromisoformat(occ.get("expiration"))
        dte = (exp.date() - datetime.now(timezone.utc).date()).days
    except Exception:
        dte = None
    return {
        "entry": getattr(guard, "entry", None), "qty": getattr(guard, "qty", None),
        "direction": occ.get("direction"), "underlying_symbol": occ.get("root"),
        "contract_symbol": sym, "entry_timestamp": getattr(guard, "opened", None),
        "strike": occ.get("strike"), "expiration": occ.get("expiration"), "dte": dte,
        # greeks/IV: not tracked by the live Guard → explicit null (no fabrication)
        "delta": None, "gamma": None, "theta": None, "iv": None,
        "rules_version": getattr(guard, "EXECUTION_MODE", "WHOLE_POSITION_RATCHET"),
        "guard_config_version": "ladder-" + "-".join(str(r["tol"]) for r in getattr(guard, "ladder", [])),
    }


def _quote_obs(platform, sym, underlying, cfg, seq, entry_epoch=None):
    """One synchronized read-only observation via the Wave quote helper."""
    import wave_quotes as wq
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    # minimal session ctx; the study uses executable bid, not session gating
    sess = {"market_open": True, "in_open_window": False, "in_close_window": False,
            "session_minute": 0}
    obs = wq.fetch_synchronized(
        stock_data=platform.stock_data, option_data=platform.option_data,
        feed=platform.feed, underlying_symbol=underlying, contract_symbol=sym,
        side="CALL", cfg=_wave_shim(cfg), sequence=seq, minute_buffer=[], session=sess,
        now_epoch=now.timestamp(), now_iso=now.isoformat())
    if entry_epoch is not None:
        obs["t"] = round(now.timestamp() - entry_epoch, 1)
    return obs


class _wave_shim:
    """Adapts IncubationConfig to the few fields wave_quotes reads."""
    def __init__(self, cfg):
        self.max_underlying_option_timestamp_skew_seconds = cfg.max_underlying_option_timestamp_skew_seconds
        self.max_spread_pct = 100.0
        self.intraday_atr_period = 14
        self.atr_warmup_min_bars = 5
        self.atr_full_bars = 14


def on_live_entry(platform, sym, guard):
    cfg = icfg.default_config()
    gf = _guard_fields(platform, sym, guard)
    obs = _quote_obs(platform, sym, gf["underlying_symbol"], cfg, 0)
    obs["t"] = 0.0
    tid = f"{sym}:{gf.get('entry_timestamp')}"
    result = ish.on_entry(tid, sym, gf, obs, cfg)
    return {**result, "trade_id": tid}


_cursor_lock = threading.Lock()
_cursor = 0


def _entry_epoch(rec):
    value = rec.get("created_at")
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def _same_guard(rec, guard):
    return bool(guard is not None and getattr(guard, "opened", None)
                and str(rec.get("trade_id", "")).endswith(str(guard.opened)))


def _prune_active_index(platform, active, now_epoch):
    """Remove stale index pointers without rewriting historical evidence."""
    kept = []
    open_symbols = set(getattr(platform, "_last_open_symbols", set()))
    for tid in active:
        rec = ist.load_trade(tid)
        if rec is None or rec.get("state") in ("COMPLETE", "INCOMPLETE_TERMINAL", "ABANDONED"):
            ist.unregister_active(tid)
            continue
        sym = rec.get("contract_symbol")
        guard = platform.guards.get(sym)
        age = now_epoch - (_entry_epoch(rec) or now_epoch)
        # Orphans older than entry + recovery + a safety margin cannot provide a
        # continuous qualifying path. Drop only the mutable active pointer and
        # preserve the record/path unchanged for audit.
        orphan_limit = (icfg.default_config().recovery_window_minutes + 15) * 60
        if age > orphan_limit and not _same_guard(rec, guard) and sym not in open_symbols:
            ist.append_telemetry(tid, {"kind": "maintenance",
                                       "result": "STALE_ACTIVE_INDEX_RETIRED",
                                       "age_seconds": round(age, 1)})
            ist.unregister_active(tid)
            continue
        kept.append(tid)
    return kept


def mark_live_exit(trade_id, reason, exit_price, initiated_by=None, now=None):
    """Persist the live exit immediately; forward recovery remains shadow-only."""
    rec = ist.load_trade(trade_id)
    if rec is None or rec.get("live_exit") is not None:
        return False
    now = now or datetime.now(timezone.utc)
    entry_epoch = _entry_epoch(rec) or now.timestamp()
    elapsed = max(0.0, now.timestamp() - entry_epoch)
    rec["live_exit"] = {"ts": now.isoformat(), "reason": reason,
                        "price": exit_price, "initiated_by": initiated_by}
    rec["state"] = "OBSERVING_RECOVERY"
    rec["recovery_until"] = elapsed + icfg.default_config().recovery_window_minutes * 60
    ist.save_trade(rec)
    ist.append_telemetry(trade_id, {"kind": "live_exit", "result": "RECORDED",
                                    "t": round(elapsed, 1)})
    return True


def observer_tick(platform, max_trades=1):
    if not icfg.enabled():
        return
    active = _prune_active_index(platform, ist.list_active(), time.time())
    if not active:
        return {"processed": 0, "active": 0}
    cfg = icfg.default_config()
    limit = max(1, int(max_trades))
    global _cursor
    with _cursor_lock:
        start = _cursor % len(active)
        selected = [active[(start + i) % len(active)] for i in range(min(limit, len(active)))]
        _cursor = (start + len(selected)) % len(active)
    processed = 0
    for tid in selected:
        try:
            rec = ist.load_trade(tid)
            if rec is None:
                continue
            sym = rec["contract_symbol"]
            g = platform.guards.get(sym)
            live = None
            if _same_guard(rec, g):
                snap = g.snapshot()
                live = {"state": ("done" if getattr(g, "done", False) else "guarding"),
                        "peak": snap.get("peak"),
                        "exit_event": ("live_exit" if getattr(g, "done", False) else None)}
            seq = ist.last_sequence(tid) + 1
            try:
                obs = _quote_obs(platform, sym, rec["underlying_symbol"], cfg, seq,
                                 entry_epoch=_entry_epoch(rec))
                ist.append_telemetry(tid, {"kind": "fetch", "ok": 1})
            except Exception as fe_err:
                msg = str(fe_err).lower()
                is_429 = "429" in msg or "too many requests" in msg
                ist.append_telemetry(tid, {"kind": "fetch",
                                           "http_429": 1 if is_429 else 0,
                                           "error": None if is_429 else str(fe_err)[:120]})
                continue      # honest gap; do not fabricate an observation
            ish.observe(tid, obs, cfg, live_guard=live)
            processed += 1
        except Exception:
            continue          # per-trade isolation
    return {"processed": processed, "selected": len(selected), "active": len(active)}


# read-only payloads for endpoints
def status_payload():
    incub = ist.list_active()
    wave = []
    try:
        import wave_store as ws
        wave = ws.list_active()
    except Exception:
        wave = []
    # expected added request load per poll cycle (unbatched, per the audit):
    #   ~3 per Wave position (underlying + option + 5m bars) + ~2 per incubation trade
    est_load = 3 * len(wave) + 2 * len(incub)
    return {"enabled": icfg.enabled(), "cohort_id": icfg.COHORT_ID,
            "cohort_locked": icfg.default_config().cohort_ready(),
            "validation_passed": ist.validation_passed(),
            "active_incubation_observations": len(incub),
            "active_wave_observations": len(wave),
            "estimated_added_requests_per_cycle": est_load,
            "active_trades": incub}


def report_payload():
    return icoh.build_report()


def proposal_payload():
    return icoh.propose_registration()
