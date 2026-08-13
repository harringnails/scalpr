"""
LABEL LIFECYCLE MANAGER (`label-lifecycle-v0`) for Scalpr Intelligence Phase 1.

This is NOT a one-time nightly calculation. It is a lifecycle manager for
per-contract target-before-stop labels. A label for a multi-session holding
horizon may stay unresolved for several sessions; each post-close run advances
whatever it can and leaves the rest PENDING.

Guarantees (per the operator's spec):
  * LABEL ONLY FROM A FROZEN SNAPSHOT. It reads the immutable decision-time
    feature snapshots written by feature_engine.persist_feature_snapshot and
    NEVER regenerates or mutates them. The candidate universe is the frozen
    contract set inside each snapshot.
  * NO FUTURE-DATA LEAK. Labels use only bars strictly after the recorded
    decision timestamp.
  * IDEMPOTENT. Canonical identity is
        (ticker, decision_timestamp, schema_version, label_policy_version, contract).
    A rerun with identical inputs/result is a no-op. A rerun that produces a
    DIFFERENT result for an already-FINAL label is written as a versioned
    correction carrying prior_label_hash, new_label_hash, correction_reason,
    calculation_timestamp and code_version — both records are preserved
    (append-only); the older is never silently overwritten.
  * OBSERVATION HORIZON RESPECTED. Labels whose horizon has not fully elapsed
    are PENDING; they finalize only once the target/stop horizon (measured in
    regular-session forward minutes) has elapsed, the contract expires, or a
    hit occurs — supporting horizons that span one or more later sessions.
  * PROXY DISTINCTION PRESERVED. Every label stays
    label_basis="delta_gamma_proxy", realized_option_path=false. Automation
    does not make the proxy look more authoritative than it is.
  * FAILURE ISOLATION. A failure for one ticker/contract records an
    ERROR_RETRYABLE label and continues; it never aborts the rest of the run.
  * NON-QUALIFYING. formal_cohort_eligible is permanently False. No ML, no
    calibrated probabilities.

States: PENDING · FINAL · UNLABELABLE · SUPERSEDED · ERROR_RETRYABLE
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import feature_engine as fe

LABEL_POLICY_VERSION = "scalpr-intel-label-v0"     # bumping this triggers correction recompute
OUTCOME_ENGINE_VERSION = "label-lifecycle-v0"
LIFECYCLE_LABELS_LOG = "scalpr_intel_labels_lifecycle_v0.jsonl"

# ── collision policy (versioned, conservative) ──────────────────────────────
# When a bar's proxied high reaches the target AND its low reaches the stop in
# the SAME bar, OHLC does not reveal intrabar ordering. We therefore NEVER infer
# the favorable (target-first) outcome. The active policy resolves the ambiguity
# conservatively as a STOP (worst realistic case) for P&L, records
# collision_result="ambiguous_same_bar", and flags counts_as_target_win=False so
# downstream analysis can also simply EXCLUDE collisions if it prefers.
#   options: "stop_first" (active) · "ambiguous_exclude" · "target_first" (never)
COLLISION_POLICY = "stop_first"

PENDING, FINAL, UNLABELABLE, SUPERSEDED, ERROR_RETRYABLE = (
    "PENDING", "FINAL", "UNLABELABLE", "SUPERSEDED", "ERROR_RETRYABLE")
# Materially-stale decision quote: distinct terminal state so it can be reported
# and excluded separately from structurally-unlabelable and from valid labels.
UNLABELABLE_STALE = "UNLABELABLE_STALE"
_TERMINAL = {FINAL, UNLABELABLE, UNLABELABLE_STALE}

# Fields that define the deterministic OUTCOME (hashed for idempotency). Excludes
# volatile bookkeeping like calculation_timestamp so identical inputs hash equal.
_HASH_FIELDS = ("status", "target_before_stop", "stop_before_target", "first_hit",
                "collision_result", "first_target_hit_ts", "first_stop_hit_ts",
                "minutes_to_first_hit", "mfe_pct", "mae_pct", "gross_return_pct",
                "net_return_pct", "valid_bars", "forward_window_start",
                "forward_window_end", "terminal_timestamp", "exit_reason",
                "truncated_at_expiry", "feature_snapshot_hash",
                "contract_snapshot_hash", "label_policy_version",
                "outcome_engine_version", "label_basis", "realized_option_path",
                "collision_policy", "counts_as_target_win", "quote_quality",
                "quote_bucket")


# ── helpers ─────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _parse_ts(s):
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _label_hash(rec):
    return fe.canonical_hash({k: rec.get(k) for k in _HASH_FIELDS})


def canonical_key(ticker, decision_ts, contract_symbol):
    return f"{ticker}|{decision_ts}|{fe.SCHEMA_VERSION}|{LABEL_POLICY_VERSION}|{contract_symbol}"


# ── the core per-contract compute (frozen snapshot → label) ─────────────────

def compute_contract_label(snap, contract, forward_bars, now=None):
    """Pure. Given a frozen snapshot, one frozen contract, and the forward
    UNDERLYING bars (each {ts, high, low, close}, already restricted to ts >
    decision_timestamp and to regular sessions), produce the label dict. Uses the
    delta-gamma proxy; ignores IV/theta (flagged). No future-data leak: caller
    supplies only forward bars."""
    now = now or _now()
    decision_ts = snap.get("decision_timestamp")
    horizon = int(snap.get("label_horizon_min") or fe.LABEL_HORIZON_MIN)
    target_pct = float(snap.get("label_target_pct") or fe.LABEL_TARGET_PCT)
    stop_pct = float(snap.get("label_stop_pct") or fe.LABEL_STOP_PCT)
    u0 = snap.get("underlying_at_decision")
    entry_mid = contract.get("mid")
    delta = contract.get("delta")
    gamma = contract.get("gamma")
    is_call = contract.get("type") == "C"
    expiry = contract.get("expiry")

    base = {
        "ticker": snap.get("ticker"),
        "decision_timestamp": decision_ts,
        "contract_symbol": contract.get("symbol"),
        "schema_version": fe.SCHEMA_VERSION,
        "label_policy_version": LABEL_POLICY_VERSION,
        "outcome_engine_version": OUTCOME_ENGINE_VERSION,
        "formal_cohort_eligible": False,
        "label_basis": "delta_gamma_proxy",
        "realized_option_path": False,
        "proxy_ignores": ["iv_change", "theta_decay", "real_bid_ask_at_exit", "real_fills"],
        "feature_snapshot_hash": snap.get("feature_snapshot_hash"),
        "contract_snapshot_hash": contract.get("contract_snapshot_hash"),
        "horizon_min": horizon, "target_pct": target_pct, "stop_pct": stop_pct,
        # defaults (overwritten below)
        "first_target_hit_ts": None, "first_stop_hit_ts": None,
        "collision_result": None, "minutes_to_first_hit": None,
        "mfe_pct": None, "mae_pct": None, "gross_return_pct": None,
        "net_return_pct": None, "valid_bars": 0, "forward_window_start": None,
        "forward_window_end": None, "terminal_timestamp": None,
        "target_before_stop": None, "stop_before_target": None, "first_hit": None,
        "exit_reason": None, "truncated_at_expiry": False,
        "unlabelable_reason": None,
        "collision_policy": COLLISION_POLICY,
        "counts_as_target_win": None,
        # frozen decision-time quote quality (carried from the snapshot)
        "quote_quality": contract.get("quote_quality"),
        "quote_quality_warning": contract.get("quote_quality_warning"),
        "quote_age_sec": contract.get("quote_age_sec"),
        "quote_bucket": contract.get("quote_bucket"),
    }

    # decision-time quote quality gate:
    #  * materially stale → UNLABELABLE_STALE (a distinct terminal state, reported
    #    and excluded separately). The contract STAYS frozen in the universe.
    #  * crossed/missing/unusable → UNLABELABLE (structural).
    #  * locked / minor-stale remain labelable, keeping their warning (above).
    if contract.get("quote_quality") == "stale":
        base.update({"status": UNLABELABLE_STALE,
                     "unlabelable_reason": f"materially_stale_quote_age_{contract.get('quote_age_sec')}s"})
        return base
    if contract.get("quote_quality") in ("crossed", "missing", "unusable"):
        base.update({"status": UNLABELABLE,
                     "unlabelable_reason": f"bad_decision_quote_{contract.get('quote_quality')}"})
        return base

    # inputs that make a proxy label impossible, ever → UNLABELABLE
    if entry_mid is None or entry_mid <= 0 or delta is None or u0 is None:
        base.update({"status": UNLABELABLE,
                     "unlabelable_reason": "missing_entry_mid_delta_or_underlying"})
        return base

    # cap the observable window at the option's expiry day (past expiry the option
    # no longer exists — observing the underlying further is meaningless)
    exp_date = None
    try:
        exp_date = datetime.fromisoformat(str(expiry)).date() if expiry else None
    except Exception:
        exp_date = None
    bars = []
    for b in (forward_bars or []):
        ts = _parse_ts(b.get("ts"))
        if ts is None:
            continue
        if exp_date and ts.date() > exp_date:
            continue
        bars.append({"ts": ts, "high": b["high"], "low": b["low"], "close": b["close"]})
    bars.sort(key=lambda x: x["ts"])
    now_past_expiry = bool(exp_date and now.date() > exp_date)

    if not bars:
        if now_past_expiry:
            base.update({"status": UNLABELABLE,
                         "unlabelable_reason": "expired_with_no_forward_bars"})
        else:
            base.update({"status": PENDING})   # waiting for forward bars to arrive
        return base

    window = bars[:horizon]                     # first `horizon` forward session minutes
    target = entry_mid * (1 + target_pct)
    stop = entry_mid * (1 - stop_pct)
    mfe = mae = 0.0
    first_tgt_ts = first_stop_ts = None
    first_hit = None
    hit_ts = None
    hit_idx = None
    for i, b in enumerate(window):
        du_hi = b["high"] - u0
        du_lo = b["low"] - u0
        p_hi = fe._proxy_option_price(entry_mid, delta, gamma, du_hi if is_call else du_lo)
        p_lo = fe._proxy_option_price(entry_mid, delta, gamma, du_lo if is_call else du_hi)
        mfe = max(mfe, (p_hi - entry_mid) / entry_mid)
        mae = min(mae, (p_lo - entry_mid) / entry_mid)
        tgt = p_hi >= target
        stp = p_lo <= stop
        if tgt and first_tgt_ts is None:
            first_tgt_ts = b["ts"]
        if stp and first_stop_ts is None:
            first_stop_ts = b["ts"]
        if first_hit is None and (tgt or stp):
            first_hit = ("ambiguous_same_bar" if (tgt and stp)
                         else "stop" if stp else "target")
            hit_ts, hit_idx = b["ts"], i

    hit_occurred = first_hit is not None
    filled = len(bars) >= horizon
    horizon_complete = hit_occurred or filled or now_past_expiry

    # exit price + return
    if first_hit == "target":
        exit_px, exit_reason = target, "target"
    elif first_hit == "stop":
        exit_px, exit_reason = stop, "stop"
    elif first_hit == "ambiguous_same_bar":
        exit_px, exit_reason = stop, "ambiguous_same_bar"     # never scored as a win
    else:
        last = window[-1]
        du = last["close"] - u0
        exit_px = fe._proxy_option_price(entry_mid, delta, gamma, du if is_call else -du)
        exit_reason = "horizon_end" if filled else ("expiry_truncation" if now_past_expiry else "in_progress")
    gross = exit_px - entry_mid
    costs = fe.LABEL_COST_BPS / 1e4 * entry_mid

    if hit_occurred:
        terminal_ts = hit_ts
    elif filled:
        terminal_ts = window[horizon - 1]["ts"] if len(window) >= horizon else window[-1]["ts"]
    elif now_past_expiry:
        terminal_ts = window[-1]["ts"]
    else:
        terminal_ts = None

    base.update({
        "status": FINAL if horizon_complete else PENDING,
        "target_before_stop": first_hit == "target",
        "stop_before_target": first_hit in ("stop", "ambiguous_same_bar"),  # collision → stop_first
        "first_hit": first_hit,
        "collision_result": first_hit or "no_hit",
        "counts_as_target_win": first_hit == "target",   # collision never a win
        "first_target_hit_ts": first_tgt_ts.isoformat() if first_tgt_ts else None,
        "first_stop_hit_ts": first_stop_ts.isoformat() if first_stop_ts else None,
        "minutes_to_first_hit": (hit_idx + 1) if hit_idx is not None else None,
        "mfe_pct": round(mfe, 6), "mae_pct": round(mae, 6),
        "gross_return_pct": round(gross / entry_mid, 6),
        "net_return_pct": round((gross - costs) / entry_mid, 6),
        "valid_bars": len(window),
        "forward_window_start": window[0]["ts"].isoformat(),
        "forward_window_end": window[-1]["ts"].isoformat(),
        "terminal_timestamp": terminal_ts.isoformat() if terminal_ts else None,
        "exit_reason": exit_reason,
        "truncated_at_expiry": bool(now_past_expiry and not filled and not hit_occurred),
        "costs_note": "PLACEHOLDER cost model; realized fills under a later label version",
    })
    return base


# ── append-only canonical store ─────────────────────────────────────────────

def _read_records(path=LIFECYCLE_LABELS_LOG):
    # tolerant read: a crash-truncated or corrupt line is skipped, not fatal
    return list(fe._iter_jsonl(path))


def _latest_by_key(records):
    """Latest (append-order) record per canonical key."""
    latest = {}
    for r in records:
        latest[r.get("canonical_key")] = r     # later lines win (append-only)
    return latest


def _write_record(rec, path=LIFECYCLE_LABELS_LOG):
    return fe._atomic_append(path, rec)         # crash-safe append + fsync


def _finalize_record(label, key, prior=None, correction_reason=None):
    """Attach canonical bookkeeping + hash. If `prior` is a differing FINAL
    record, mark this as a versioned correction."""
    rec = dict(label)
    rec["canonical_key"] = key
    rec["calculation_timestamp"] = _now().isoformat()
    rec["label_hash"] = _label_hash(label)
    rec["is_correction"] = False
    if prior is not None:
        rec["is_correction"] = True
        rec["supersedes_hash"] = prior.get("label_hash")
        rec["prior_label_hash"] = prior.get("label_hash")
        rec["new_label_hash"] = rec["label_hash"]
        rec["correction_reason"] = correction_reason or "recomputed_result_differs"
        rec["prior_status"] = prior.get("status")
    return rec


# ── the lifecycle run (wired into the existing close/outcome workflow) ──────

def run_lifecycle(bars_provider, ticker=None, market_date=None, now=None,
                  snapshots_path=fe.FEATURE_SNAPSHOTS_LOG,
                  labels_path=LIFECYCLE_LABELS_LOG):
    """Advance every label that can be advanced.

    `bars_provider(ticker, decision_ts_utc, now_utc) -> [{ts, high, low, close}]`
    must return regular-session UNDERLYING minute bars strictly after the
    decision timestamp (multi-session as needed), or raise on transient failure.

    Processes ALL snapshots by default (so multi-session PENDING labels are
    revisited on later closes). Returns a completion summary. Per-snapshot and
    per-contract failures are isolated: they record ERROR_RETRYABLE and the run
    continues.
    """
    now = now or _now()
    snaps = fe.read_feature_snapshots(snapshots_path, ticker=ticker, market_date=market_date)
    existing = _latest_by_key(_read_records(labels_path))

    summary = {"snapshots_examined": 0, "contracts_examined": 0,
               "labels_finalized": 0, "labels_pending": 0, "labels_unlabelable": 0,
               "labels_unlabelable_stale": 0,
               "corrections": 0, "retries": 0, "errors": 0, "noops": 0,
               "outcome_engine_version": OUTCOME_ENGINE_VERSION,
               "label_policy_version": LABEL_POLICY_VERSION,
               "formal_cohort_eligible": False}

    for snap in snaps:
        summary["snapshots_examined"] += 1
        tkr = snap.get("ticker")
        decision_ts = snap.get("decision_timestamp")
        dts = _parse_ts(decision_ts)
        # fetch forward bars once per snapshot (failure isolated to this snapshot)
        try:
            fbars = bars_provider(tkr, dts, now) if dts else []
            provider_ok = True
        except Exception as e:
            fbars, provider_ok = [], False
            provider_err = str(e)[:200]

        for contract in snap.get("contract_universe", []):
            summary["contracts_examined"] += 1
            sym = contract.get("symbol")
            key = canonical_key(tkr, decision_ts, sym)
            prior = existing.get(key)

            # skip already-terminal labels whose engine/policy version is unchanged
            if prior and prior.get("status") in _TERMINAL:
                if (prior.get("outcome_engine_version") == OUTCOME_ENGINE_VERSION
                        and prior.get("label_policy_version") == LABEL_POLICY_VERSION):
                    summary["noops"] += 1
                    continue
                # else: versioned recompute below may issue a correction

            # provider failed → retryable, do not fabricate; write once
            if not provider_ok:
                if prior and prior.get("status") == ERROR_RETRYABLE:
                    summary["noops"] += 1
                    continue
                err = {"ticker": tkr, "decision_timestamp": decision_ts,
                       "contract_symbol": sym, "schema_version": fe.SCHEMA_VERSION,
                       "label_policy_version": LABEL_POLICY_VERSION,
                       "outcome_engine_version": OUTCOME_ENGINE_VERSION,
                       "formal_cohort_eligible": False, "status": ERROR_RETRYABLE,
                       "error_reason": f"bars_provider_failed: {provider_err}",
                       "feature_snapshot_hash": snap.get("feature_snapshot_hash"),
                       "contract_snapshot_hash": contract.get("contract_snapshot_hash")}
                if _write_record(_finalize_record(err, key), labels_path):
                    summary["retries"] += 1
                continue

            # compute (isolate per-contract failures)
            try:
                label = compute_contract_label(snap, contract, fbars, now=now)
            except Exception as e:
                if prior and prior.get("status") == ERROR_RETRYABLE:
                    summary["noops"] += 1
                    continue
                err = {"ticker": tkr, "decision_timestamp": decision_ts,
                       "contract_symbol": sym, "schema_version": fe.SCHEMA_VERSION,
                       "label_policy_version": LABEL_POLICY_VERSION,
                       "outcome_engine_version": OUTCOME_ENGINE_VERSION,
                       "formal_cohort_eligible": False, "status": ERROR_RETRYABLE,
                       "error_reason": f"compute_failed: {str(e)[:200]}",
                       "feature_snapshot_hash": snap.get("feature_snapshot_hash"),
                       "contract_snapshot_hash": contract.get("contract_snapshot_hash")}
                if _write_record(_finalize_record(err, key), labels_path):
                    summary["retries"] += 1
                summary["errors"] += 1
                continue

            status = label["status"]
            new_hash = _label_hash(label)

            # ── idempotent write decisions ──
            if prior is None:
                if _write_record(_finalize_record(label, key), labels_path):
                    _bump(summary, status)
                continue

            pstatus = prior.get("status")
            if pstatus in _TERMINAL:
                # only reachable when a version changed; correction iff hash differs
                if prior.get("label_hash") == new_hash:
                    summary["noops"] += 1
                elif status in _TERMINAL:
                    rec = _finalize_record(label, key, prior=prior,
                                           correction_reason="version_recompute_differs")
                    if _write_record(rec, labels_path):
                        summary["corrections"] += 1
                        _bump(summary, status)
                else:
                    summary["noops"] += 1        # don't regress a terminal to pending
                continue

            # prior is PENDING or ERROR_RETRYABLE → advance
            if status == PENDING:
                summary["labels_pending"] += 1   # still pending; no duplicate write
                summary["noops"] += 1
                continue
            # PENDING/ERROR → terminal: normal finalization (not a correction)
            if _write_record(_finalize_record(label, key), labels_path):
                _bump(summary, status)

    return summary


def _bump(summary, status):
    if status == FINAL:
        summary["labels_finalized"] += 1
    elif status == PENDING:
        summary["labels_pending"] += 1
    elif status == UNLABELABLE:
        summary["labels_unlabelable"] += 1
    elif status == UNLABELABLE_STALE:
        summary["labels_unlabelable_stale"] += 1


def canonical_labels(path=LIFECYCLE_LABELS_LOG, ticker=None):
    """Latest canonical label per key — the readable dataset for rules-score vs
    outcome comparison. Corrections supersede the records they correct."""
    recs = _read_records(path)
    if ticker:
        recs = [r for r in recs if r.get("ticker") == ticker.upper()]
    return list(_latest_by_key(recs).values())
