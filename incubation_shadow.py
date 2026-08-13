"""
ENTRY_INCUBATION_SHADOW capture lifecycle. SHADOW / READ-ONLY.

Records the immutable entry snapshot and ONE canonical forward option path per
real Standard-mode trade, continuing PAST the live Guard exit through the
versioned recovery window. Variant-agnostic: the activation × hard-stop factorial
is replayed OFFLINE at finalize (incubation_cohort). Never touches the live Guard,
orders, or Standard mode.

States: OBSERVING_PRE_EXIT → OBSERVING_RECOVERY → COMPLETE | INCOMPLETE_TERMINAL | ABANDONED
"""
from datetime import datetime, timezone

import feature_engine as fe
import incubation_store as ist
from incubation_config import INCUBATION_SHADOW_VERSION

_BAD_Q = ("crossed", "missing", "unusable", "stale")


def _now():
    return datetime.now(timezone.utc)


def observation_identity(tid, obs, seq):
    return fe.canonical_hash([tid, obs.get("option_quote_timestamp"),
                              obs.get("underlying_quote_timestamp"), seq,
                              INCUBATION_SHADOW_VERSION])


def build_entry_snapshot(trade_id, position_id, guard_fields, quote_obs, cfg):
    """Freeze decision-time context at the confirmed live fill. Unavailable values
    stay explicit null with availability status — no fabricated greeks/timestamps."""
    snap = {
        "trade_id": trade_id, "position_id": position_id,
        "shadow_version": INCUBATION_SHADOW_VERSION,
        "study_version": cfg.study_version,
        "entry_timestamp": guard_fields.get("entry_timestamp"),
        "underlying_symbol": guard_fields.get("underlying_symbol"),
        "option_contract_symbol": guard_fields.get("contract_symbol"),
        "direction": guard_fields.get("direction"),
        "entry_quantity": guard_fields.get("qty"),
        "actual_fill_price": guard_fields.get("entry"),
        # option/underlying market at entry (field-level source + ts; nulls if absent)
        "option_bid": quote_obs.get("option_bid"), "option_ask": quote_obs.get("option_ask"),
        "option_mid": quote_obs.get("option_mid"),
        "option_quote_timestamp": quote_obs.get("option_quote_timestamp"),
        "underlying_price": quote_obs.get("underlying_price"),
        "underlying_quote_timestamp": quote_obs.get("underlying_quote_timestamp"),
        "spread_pct": quote_obs.get("spread_pct"),
        "delta": guard_fields.get("delta"), "gamma": guard_fields.get("gamma"),
        "theta": guard_fields.get("theta"), "implied_volatility": guard_fields.get("iv"),
        "dte": guard_fields.get("dte"), "strike": guard_fields.get("strike"),
        "expiration": guard_fields.get("expiration"),
        "vwap_relationship": quote_obs.get("above_vwap_side_ok"),
        "five_minute_atr": quote_obs.get("atr_value"),
        "atr_quality": quote_obs.get("atr_quality"),
        "rules_version": guard_fields.get("rules_version"),
        "guard_config_version": guard_fields.get("guard_config_version"),
        "incubation_study_version": cfg.study_version,
        "availability": {k: (guard_fields.get(k) is not None) for k in
                         ("delta", "gamma", "theta", "iv", "dte", "strike", "expiration")},
        "sources": quote_obs.get("sources"),
    }
    snap["snapshot_hash"] = fe.canonical_hash({k: v for k, v in snap.items() if k != "snapshot_hash"})
    return snap


def on_entry(trade_id, position_id, guard_fields, quote_obs, cfg, store=ist):
    """Called (gated, exception-isolated) right after the live Guard is created.
    Persists the immutable snapshot + opens the trade record, THEN registers it
    for forward observation. Read-only w.r.t. the live position."""
    # study role: the FIRST trade (until one clean validation passes) is an
    # OPERATIONAL_VALIDATION run — it exercises the full lifecycle but is excluded
    # from Cohort A metrics/gates.
    if store.validation_passed():
        role = {"study_role": "COHORT", "cohort_eligible": True, "exclusion_reason": None}
    else:
        role = {"study_role": "OPERATIONAL_VALIDATION", "cohort_eligible": False,
                "exclusion_reason": "INITIAL_PIPELINE_VALIDATION"}
    snap = build_entry_snapshot(trade_id, position_id, guard_fields, quote_obs, cfg)
    snap.update(role)
    store.save_snapshot(snap)
    store.append_observation(trade_id, {**_obs_record(quote_obs, 0),
                                        "observation_id": observation_identity(trade_id, quote_obs, 0)})
    rec = {"trade_id": trade_id, "position_id": position_id,
           "state": "OBSERVING_PRE_EXIT", "shadow_version": INCUBATION_SHADOW_VERSION,
           "record_schema_version": "incubation-record-v0.1-study-role",
           "snapshot_hash": snap["snapshot_hash"], "entry": guard_fields.get("entry"),
           "qty": guard_fields.get("qty"), "direction": guard_fields.get("direction"),
           "underlying_symbol": guard_fields.get("underlying_symbol"),
           "contract_symbol": guard_fields.get("contract_symbol"),
           "live_exit": None, "last_processed_sequence": 0,
           **role, "created_at": _now().isoformat()}
    store.save_trade(rec)
    store.register_active(trade_id)
    return {"created": True, "snapshot_hash": snap["snapshot_hash"]}


def _obs_record(obs, seq, live_guard=None):
    return {
        "observation_sequence": seq,
        "t": obs.get("t"),                        # seconds from entry (filled by caller)
        "ts": obs.get("ts"),
        "option_bid": obs.get("option_bid"), "option_ask": obs.get("option_ask"),
        "option_mid": obs.get("option_mid"),
        "option_quote_timestamp": obs.get("option_quote_timestamp"),
        "underlying_price": obs.get("underlying_price"),
        "underlying_quote_timestamp": obs.get("underlying_quote_timestamp"),
        "quote_age_sec": obs.get("quote_age_sec"), "spread_pct": obs.get("spread_pct"),
        "timestamp_skew_seconds": obs.get("timestamp_skew_seconds"),
        "quote_quality": obs.get("quote_quality"),
        "unsynchronized": obs.get("unsynchronized"),
        "live_guard_state": (live_guard or {}).get("state"),
        "live_guard_peak": (live_guard or {}).get("peak"),
        "live_exit_event": (live_guard or {}).get("exit_event"),
    }


def observe(trade_id, obs, cfg, live_guard=None, store=ist, now=None):
    """Append one forward observation (deduped by option quote timestamp).
    Advances the recovery-window state machine. Returns the applied/no-op result."""
    now = now or _now()
    rec = store.load_trade(trade_id)
    if rec is None or rec["state"] in ("COMPLETE", "INCOMPLETE_TERMINAL", "ABANDONED"):
        return {"applied": False, "code": "INACTIVE"}
    seq = store.last_sequence(trade_id) + 1
    last_q = store.last_option_quote_ts(trade_id)
    if obs.get("option_quote_timestamp") and obs["option_quote_timestamp"] == last_q:
        store.append_telemetry(trade_id, {"kind": "observe", "result": "DUPLICATE"})
        return {"applied": False, "code": "DUPLICATE_OBSERVATION"}      # same quote → no-op
    oid = observation_identity(trade_id, obs, seq)
    store.append_observation(trade_id, {**_obs_record(obs, seq, live_guard), "observation_id": oid})

    # live exit captured → enter recovery window
    if live_guard and live_guard.get("exit_event") and rec.get("live_exit") is None:
        rec["live_exit"] = {"ts": obs.get("ts"), "reason": live_guard["exit_event"],
                            "price": obs.get("option_bid")}
        rec["state"] = "OBSERVING_RECOVERY"
        rec["recovery_until"] = obs.get("t", 0) + cfg.recovery_window_minutes * 60

    # terminal checks (recovery window elapsed / market close / expiry / data-failure)
    terminal = None
    if rec.get("state") == "OBSERVING_RECOVERY" and rec.get("recovery_until") is not None \
            and obs.get("t", 0) >= rec["recovery_until"]:
        terminal = "recovery_window_complete"
    if obs.get("market_closed"):
        terminal = "market_close"
    if obs.get("expired"):
        terminal = "expiration"

    rec["last_processed_sequence"] = seq
    store.append_telemetry(trade_id, {"kind": "observe", "result": "OK",
                                      "stale": obs.get("quote_quality") in _BAD_Q,
                                      "unsync": bool(obs.get("unsynchronized")),
                                      "t": obs.get("t")})
    if terminal:
        rec["state"] = "COMPLETE" if rec.get("live_exit") else "INCOMPLETE_TERMINAL"
        rec["terminal_reason"] = terminal
        store.unregister_active(trade_id)
    store.save_trade(rec)
    return {"applied": True, "state": rec["state"], "sequence": seq}


def reload_active(store=ist):
    """Restart recovery: reload active trades; never manufacture missed
    observations; a trade whose recovery window elapsed during downtime is
    ABANDONED (excluded from fully-observed). Dedupe prevents double records."""
    out = []
    for tid in store.list_active():
        rec = store.load_trade(tid)
        if rec is None:
            store.unregister_active(tid); continue
        rec["restarted_at"] = _now().isoformat()
        rec["gap_after_restart"] = True     # honest gap marker; not interpolated
        store.save_trade(rec)
        out.append(rec)
    return out
