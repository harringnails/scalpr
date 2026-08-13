"""
SESSION SNAPSHOT — immutable, observational end-of-session archive.

Purpose: preserve exactly what the system knew at the end of each market
session, so that "the effect is repeatable across materially different
conditions" can be evaluated later from an audit trail rather than reconstructed
from memory. This module NEVER retrains, changes thresholds, alters model
settings, or touches trades. Every record is written append-only and is
immutable once written.

Three dates are kept distinct on purpose (a rerun next morning must never be
mistaken for a genuine end-of-day result):
  * market_date          — the trading session being evaluated
  * evaluation_cutoff    — the last provider_ts allowed into the analysis
  * snapshot_created_at  — when this file was actually generated

Freeze rule (enforced): no feature, state, baseline, null test, or verdict may
use an event whose provider_ts is after evaluation_cutoff. The file may be
generated later (e.g. cutoff+15min to let late arrivals be accounted for), but
the analytical dataset is frozen at the cutoff. Late arrivals show up in timing
diagnostics; they do not enter model inputs.

Two hashes form the reproducibility chain
    raw events → finalized bars/features → research result:
  * raw_event_manifest_hash — which source records were available (frozen rows)
  * analysis_dataset_hash    — the exact ordered bars/features the model used
"""

import contextlib
import csv
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone, time as dtime
from pathlib import Path

try:
    import fcntl                              # POSIX (macOS/Linux) — advisory file lock
    _HAVE_FCNTL = True
except Exception:                             # pragma: no cover
    _HAVE_FCNTL = False

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                      # pragma: no cover
    ET = timezone.utc

import regime_model as rm
import regime_research as rr
import bar_builder as bb

SNAPSHOT_DIR = Path(__file__).parent / "research_snapshots"
SNAPSHOT_TYPE_DEFAULT = "regular"
PRIMARY_HORIZON = rr.PRIMARY_HORIZON

# Required keys a file must have to be treated as a valid snapshot; anything
# missing them (a truncated/partial/temp artifact) is ignored by readers.
_REQUIRED_KEYS = ("symbol", "market_date", "model_version", "snapshot_type", "verdict", "gates")


@contextlib.contextmanager
def _snapshot_lock():
    """Cross-process advisory lock so concurrent generators (e.g. multiple
    uvicorn workers each running a scheduler thread) can't race on the same
    session. Combined with idempotency this guarantees one record per key.
    Degrades to a no-op only if fcntl is unavailable (non-POSIX)."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    if not _HAVE_FCNTL:
        yield
        return
    lock_path = SNAPSHOT_DIR / ".snapshot.lock"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def _atomic_write_json(path, obj):
    """Write to a temp file, flush+fsync, then os.replace (atomic rename) so a
    crash mid-write can never leave a truncated JSON artifact at `path`."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)   # atomic on POSIX

# ── pre-registered multi-session acceptance rule (fixed BEFORE real data) ────
# These are provisional research thresholds, recorded here so they cannot be
# quietly tuned to fit whatever the accumulated sessions happen to show.
MIN_ELIGIBLE_SESSIONS = 20
MIN_POSITIVE_EDGE_RATE = 0.65
MIN_BASELINE_WIN_RATE = 0.60
MIN_TIME_AWARE_NULL_PASS_RATE = 0.50
MIN_MEDIAN_ABSOLUTE_EDGE = 0.03
MIN_MEDIAN_INCREMENTAL_EDGE = 0.01
MAX_TIMING_FAILURE_RATE = 0.05
AGG_WINDOWS = [5, 10, 20]


# ── provenance ───────────────────────────────────────────────────────────

def _code_fingerprint():
    """git sha if this is a repo, else a hash of the source files so the code
    state behind a snapshot is always identifiable."""
    here = Path(__file__).parent
    try:
        sha = subprocess.run(["git", "-C", str(here), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        if sha.returncode == 0 and sha.stdout.strip():
            return sha.stdout.strip()
    except Exception:
        pass
    h = hashlib.sha256()
    for name in sorted(["regime_model.py", "regime_research.py", "bar_builder.py",
                        "session_snapshot.py", "precheck.py"]):
        p = here / name
        if p.exists():
            h.update(p.read_bytes())
    return "nogit-" + h.hexdigest()[:16]


# ── frozen dataset construction ──────────────────────────────────────────

def _event_time(row):
    """Provider event time if present, else receipt time (unaudited fallback)."""
    for key in ("provider_ts", "utc_time"):
        v = row.get(key)
        if v:
            try:
                return datetime.fromisoformat(v)
            except ValueError:
                continue
    return None


def _frozen_rows(symbol, session_open, cutoff):
    """Rows for the symbol whose event time is within [session_open, cutoff].
    Anything after the cutoff is excluded from analysis entirely."""
    if not rm.TICK_LOG.exists():
        return [], None
    rows, fields = [], None
    with rm.TICK_LOG.open(newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        for r in reader:
            if r.get("symbol") != symbol.upper():
                continue
            et = _event_time(r)
            if et is None:
                continue
            if session_open <= et <= cutoff:
                rows.append(r)
    rows.sort(key=lambda r: _event_time(r))
    return rows, fields


def _raw_manifest_hash(rows):
    canon = sorted(
        (r.get("provider_ts", ""), r.get("utc_time", ""), r.get("bid", ""), r.get("ask", ""),
         r.get("bid_size", ""), r.get("ask_size", "")) for r in rows)
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()[:32]


def _analysis_dataset_hash(bar_times, X):
    """Hash the EXACT ordered bars/features the model consumed."""
    payload = [[int(t)] + [round(float(v), 8) for v in row]
               for t, row in zip(bar_times, X)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]


def _write_frozen_temp(rows, fields):
    tmp = SNAPSHOT_DIR / ".frozen_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / f"frozen_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.csv"
    use_fields = fields or ["utc_time", "provider_ts", "symbol", "bid", "ask",
                            "bid_size", "ask_size", "mid", "spread"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=use_fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in use_fields})
    return path


def _run_analysis_on_frozen(symbol, frozen_path):
    """Point the model modules at the frozen dataset, compute every endpoint's
    full output, then restore. Snapshots are infrequent; the brief swap is
    guarded with try/finally so the live path is always restored."""
    original = rm.TICK_LOG
    try:
        rm.TICK_LOG = frozen_path
        rm._cache.clear()                 # never reuse a live-fit model in a frozen snapshot
        timing = rm.timing_report(symbol)
        regime = rm.regime_read(symbol, force=True)
        validate = rm.validate_regime(symbol)
        research = rr.research_report(symbol)
        bar_times, X = rm.build_bars(symbol)
        analysis_hash = _analysis_dataset_hash(bar_times, X)
    finally:
        rm.TICK_LOG = original
        rm._cache.clear()
    return timing, regime, validate, research, (bar_times, X), analysis_hash


# ── gate extraction ──────────────────────────────────────────────────────

def _gate_sequence(timing, regime, validate, research):
    """The full pre-verdict gate sequence — far more useful than one pass/fail
    because it says WHY (bad data vs collapsed states vs no edge vs baseline)."""
    tq = timing.get("timing_quality", {})
    diag = regime.get("diagnostics", {}) if regime.get("available") else {}
    prim = research.get("primary_horizon_result", {}) if research.get("available") else {}
    return {
        "validation_data_ready": research.get("status") == "validation_ready",
        "timing_quality_passed": (tq.get("passed") if timing.get("audited") else None),
        "state_health_passed": (not diag.get("collapsed_or_dominated", True)) if diag else None,
        "positive_predictive_value": prim.get("hmm_has_positive_predictive_value"),
        "beats_primary_baseline": prim.get("hmm_beats_primary_baseline_with_margin"),
        "circular_null_passed": (prim.get("null_circular_shift") or {}).get("exceeds_null_p95"),
        # the session-aware block bootstrap is the CONTROLLING null in v3
        "block_null_passed": (prim.get("null_session_block") or {}).get("exceeds_null_p95"),
        # a single session can never establish multi-day persistence — the
        # aggregator owns this gate; it is always False in a daily snapshot.
        "multi_day_requirement_passed": False,
    }


def _default_session_bounds(market_date, session_open, session_close, cutoff, now):
    md = market_date
    if isinstance(md, str):
        md = datetime.fromisoformat(md).date()
    if md is None:
        md = datetime.now(ET).date()
    if session_open is None:
        session_open = datetime.combine(md, dtime(9, 30), tzinfo=ET)
    if session_close is None:
        session_close = datetime.combine(md, dtime(16, 0), tzinfo=ET)
    if cutoff is None:
        cutoff = session_close
    if now is None:
        now = datetime.now(timezone.utc)
    return md, session_open, session_close, cutoff, now


# ── the writer ───────────────────────────────────────────────────────────

def _read_snapshot_file(p):
    """Parse a snapshot file, returning None for temp/partial/malformed files
    so a crash-truncated artifact can never corrupt a read."""
    if p.name.endswith(".tmp"):
        return None
    try:
        rec = json.loads(p.read_text())
    except Exception:
        return None
    if not isinstance(rec, dict) or any(k not in rec for k in _REQUIRED_KEYS):
        return None
    return rec


def _existing_regular(symbol, market_date, model_version):
    d = SNAPSHOT_DIR / symbol.upper() / str(market_date)
    if not d.exists():
        return None
    for p in sorted(d.glob("*.json")):
        rec = _read_snapshot_file(p)
        if rec is None:
            continue
        if (rec.get("model_version") == model_version
                and rec.get("snapshot_type") == SNAPSHOT_TYPE_DEFAULT
                and not rec.get("is_rerun")):
            return p, rec
    return None


def take_snapshot(symbol="SPY", market_date=None, session_open=None, session_close=None,
                  cutoff=None, snapshot_type=SNAPSHOT_TYPE_DEFAULT, reason=None,
                  force_rerun=False, now=None, calendar_verified=True):
    # The whole check-then-write is done under a cross-process lock so two
    # workers can't both pass the idempotency check and both write.
    with _snapshot_lock():
        return _take_snapshot_locked(symbol, market_date, session_open, session_close,
                                     cutoff, snapshot_type, reason, force_rerun, now,
                                     calendar_verified)


def _take_snapshot_locked(symbol, market_date, session_open, session_close, cutoff,
                          snapshot_type, reason, force_rerun, now, calendar_verified):
    md, session_open, session_close, cutoff, now = _default_session_bounds(
        market_date, session_open, session_close, cutoff, now)
    model_version = rm.MODEL_VERSION

    # idempotency: symbol + market_date + model_version + snapshot_type
    existing = _existing_regular(symbol, md, model_version)
    if existing and not force_rerun:
        path, rec = existing
        return {"created": False, "existing": True, "path": str(path),
                "verdict": rec.get("verdict", {}).get("verdict")}

    rows, fields = _frozen_rows(symbol, session_open, cutoff)
    raw_hash = _raw_manifest_hash(rows)
    frozen_path = _write_frozen_temp(rows, fields)
    try:
        timing, regime, validate, research, (bar_times, X), analysis_hash = \
            _run_analysis_on_frozen(symbol, frozen_path)
    finally:
        try:
            frozen_path.unlink()
        except OSError:
            pass

    bars_observed = len(X)
    session_seconds = (cutoff - session_open).total_seconds()
    bars_expected = max(1, int(session_seconds // bb.BAR_SECONDS))
    gaps = 0
    if bar_times:
        # count index gaps as a coarse interruption proxy
        idxs = sorted(int(t // bb.BAR_NS) if t > 1e12 else int(t) for t in bar_times)
        gaps = sum(1 for a, b in zip(idxs, idxs[1:]) if b - a > 1)

    prim = research.get("primary_horizon_result", {}) if research.get("available") else {}
    verdict = {
        "verdict": research.get("verdict") if research.get("available") else "insufficient_data",
        "primary_horizon_bars": PRIMARY_HORIZON,
        "hmm_score": prim.get("hmm_score"),
        "primary_baseline_score": prim.get("primary_baseline_score"),
        "incremental_edge": (None if prim.get("hmm_score") is None or prim.get("primary_baseline_score") is None
                             else round(prim["hmm_score"] - prim["primary_baseline_score"], 4)),
    }

    record = {
        "symbol": symbol.upper(),
        "market_date": str(md),
        "session_timezone": "America/New_York",
        "session_open": session_open.isoformat(),
        "session_close": session_close.isoformat(),
        "evaluation_cutoff": cutoff.isoformat(),
        "snapshot_created_at": now.astimezone(timezone.utc).isoformat(),
        "snapshot_type": snapshot_type,

        "model_version": model_version,
        "research_version": rr.RESEARCH_VERSION,
        "bar_builder_version": bb.BAR_BUILDER_VERSION,
        "code_commit": _code_fingerprint(),

        "primary_horizon_bars": PRIMARY_HORIZON,
        "primary_horizon_seconds": PRIMARY_HORIZON * bb.BAR_SECONDS,

        "data_coverage": {
            "bars_expected": bars_expected,
            "bars_observed": bars_observed,
            "coverage_ratio": round(bars_observed / bars_expected, 4) if bars_expected else 0.0,
            "raw_rows_frozen": len(rows),
            "bar_index_gaps": gaps,
            "market_interruptions": gaps,
        },

        # FULL endpoint outputs preserved — a diagnostic you don't yet know you
        # need is worth more than a tidy summary.
        "timing_quality": timing,
        "regime_diagnostics": regime,
        "walk_forward_results": validate,
        "research": research,
        "gates": _gate_sequence(timing, regime, validate, research),
        "verdict": verdict,

        "raw_event_manifest_hash": raw_hash,
        "analysis_dataset_hash": analysis_hash,

        # If the market calendar couldn't be verified, the cutoff may be wrong
        # (unsafe on an early-close day), so the headline verdict is not trusted
        # for the research gate — the record is still preserved for data review.
        "snapshot_status": "ok" if calendar_verified else "calendar_unverified",
        "headline_verdict_allowed": bool(calendar_verified),

        # architectural boundary — reasserted in every record on purpose
        "analysis_mode": "observational_research",
        "connected_to_execution": False,
        "connected_to_position_sizing": False,
        "connected_to_signal_weighting": False,
        "is_rerun": False,
    }

    if force_rerun and existing:
        _, prev = existing
        record["is_rerun"] = True
        record["rerun"] = {
            "reason": reason or "unspecified",
            "original_snapshot_created_at": prev.get("snapshot_created_at"),
            "code_changed": prev.get("code_commit") != record["code_commit"],
            "data_changed": prev.get("analysis_dataset_hash") != record["analysis_dataset_hash"],
            "verdict_changed": (prev.get("verdict", {}).get("verdict")
                                != record["verdict"]["verdict"]),
        }

    out_dir = SNAPSHOT_DIR / symbol.upper() / str(md)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_rerun" if record["is_rerun"] else ""
    fname = f"{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{model_version}{suffix}.json"
    out_path = out_dir / fname
    # append-only: never overwrite an existing file
    n = 1
    while out_path.exists():
        out_path = out_dir / f"{out_path.stem}.{n}.json"
        n += 1
    _atomic_write_json(out_path, record)   # temp + fsync + rename: no truncated artifacts
    return {"created": True, "is_rerun": record["is_rerun"], "path": str(out_path),
            "snapshot_status": record["snapshot_status"],
            "verdict": record["verdict"]["verdict"],
            "analysis_dataset_hash": analysis_hash, "raw_event_manifest_hash": raw_hash}


# ── multi-session aggregator (read-only over immutable snapshots) ─────────

def _load_regular_snapshots(symbol):
    """All non-rerun snapshots for a symbol, one per market_date (the earliest
    regular snapshot per date), sorted ascending by market_date."""
    base = SNAPSHOT_DIR / symbol.upper()
    if not base.exists():
        return []
    by_date = {}
    for date_dir in base.iterdir():
        if not date_dir.is_dir():
            continue
        for p in sorted(date_dir.glob("*.json")):
            rec = _read_snapshot_file(p)   # skips temp/partial/malformed
            if rec is None:
                continue
            if rec.get("is_rerun") or rec.get("snapshot_type") != SNAPSHOT_TYPE_DEFAULT:
                continue
            by_date.setdefault(rec.get("market_date"), rec)   # first (earliest) wins
    return [by_date[d] for d in sorted(by_date)]


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def _window_stats(snaps):
    """Aggregate over a list of session snapshots. Headline metrics come from
    the PRIMARY horizon only; secondary horizons are never aggregated into the
    verdict (avoids multiple-testing inflation)."""
    n = len(snaps)
    calendar_unverified = sum(1 for s in snaps if not s.get("headline_verdict_allowed", True))
    # a session feeds the research gate only if its data is ready AND its cutoff
    # is calendar-verified (an unverified early-close cutoff can't be trusted).
    eligible = [s for s in snaps if s.get("gates", {}).get("validation_data_ready")
                and s.get("headline_verdict_allowed", True)]
    ne = len(eligible)
    timing_fail = sum(1 for s in snaps if s.get("gates", {}).get("timing_quality_passed") is False)

    def gate(s, key):
        return bool(s.get("gates", {}).get(key))

    pos_edge = [s for s in eligible if gate(s, "positive_predictive_value")]
    beats = [s for s in eligible if gate(s, "beats_primary_baseline")]
    # the controlling null in v3 is the session-aware block bootstrap
    null_pass = [s for s in eligible if gate(s, "block_null_passed")]
    scores = [s.get("verdict", {}).get("hmm_score") for s in eligible]
    incr = [s.get("verdict", {}).get("incremental_edge") for s in eligible]
    signs = [1 if (v is not None and v > 0) else 0 for v in scores if v is not None]
    sign_consistency = (max(sum(signs), len(signs) - sum(signs)) / len(signs)) if signs else None
    model_versions = {s.get("model_version") for s in snaps}
    research_versions = {s.get("research_version") for s in snaps}

    stats = {
        "window_sessions": n,
        "eligible_sessions": ne,
        "calendar_unverified_sessions": calendar_unverified,
        "positive_edge_sessions": len(pos_edge),
        "beats_primary_baseline_sessions": len(beats),
        "time_aware_null_pass_sessions": len(null_pass),
        "timing_failure_sessions": timing_fail,
        "median_effect": None if _median(scores) is None else round(_median(scores), 4),
        "median_incremental_edge": None if _median(incr) is None else round(_median(incr), 4),
        "effect_sign_consistency": None if sign_consistency is None else round(sign_consistency, 3),
        "worst_session_effect": None if not scores or all(v is None for v in scores)
                                else round(min(v for v in scores if v is not None), 4),
        "model_version_consistent": len(model_versions) == 1,
        "model_versions": sorted(v for v in model_versions if v),
        "research_version_consistent": len(research_versions) == 1,
        "research_versions": sorted(v for v in research_versions if v),
    }

    # pre-registered rule — evaluated, never mutated to fit the data
    rule = {
        "enough_eligible_sessions": ne >= MIN_ELIGIBLE_SESSIONS,
        "positive_edge_rate_ok": ne > 0 and len(pos_edge) / ne >= MIN_POSITIVE_EDGE_RATE,
        "baseline_win_rate_ok": ne > 0 and len(beats) / ne >= MIN_BASELINE_WIN_RATE,
        "time_aware_null_rate_ok": ne > 0 and len(null_pass) / ne >= MIN_TIME_AWARE_NULL_PASS_RATE,
        "median_abs_edge_ok": stats["median_effect"] is not None
                              and stats["median_effect"] >= MIN_MEDIAN_ABSOLUTE_EDGE,
        "median_incremental_edge_ok": stats["median_incremental_edge"] is not None
                              and stats["median_incremental_edge"] >= MIN_MEDIAN_INCREMENTAL_EDGE,
        "timing_failure_rate_ok": n == 0 or timing_fail / n <= MAX_TIMING_FAILURE_RATE,
        "model_version_consistent": stats["model_version_consistent"],
        "research_version_consistent": stats["research_version_consistent"],
    }
    if ne < MIN_ELIGIBLE_SESSIONS:
        aggregate_status = "insufficient_sessions"
    elif all(rule.values()):
        aggregate_status = "meets_preregistered_gate"
    else:
        aggregate_status = "insufficient_consistency"
    stats["preregistered_rule"] = rule
    stats["aggregate_status"] = aggregate_status
    return stats


def aggregate_sessions(symbol="SPY", windows=None):
    """Read-only aggregation across immutable snapshots. Observational; never
    influences the model. Reports rolling windows plus the pre-registered rule."""
    windows = windows or AGG_WINDOWS
    all_snaps = _load_regular_snapshots(symbol)
    # NEVER blend research versions. v3 targets (cumulative forward log return)
    # are not comparable to v2 (isolated-bar). Aggregate ONLY the latest version;
    # older-version sessions are reported but excluded from the windows/gate.
    latest_version = all_snaps[-1].get("research_version") if all_snaps else None
    snaps = [s for s in all_snaps if s.get("research_version") == latest_version]
    excluded = len(all_snaps) - len(snaps)
    return {
        "symbol": symbol.upper(),
        "analysis_mode": "observational_research",
        "connected_to_execution": False,
        "total_sessions_on_file": len(all_snaps),
        "aggregated_research_version": latest_version,
        "sessions_excluded_other_research_versions": excluded,
        "primary_horizon_bars": PRIMARY_HORIZON,
        "preregistered_thresholds": {
            "MIN_ELIGIBLE_SESSIONS": MIN_ELIGIBLE_SESSIONS,
            "MIN_POSITIVE_EDGE_RATE": MIN_POSITIVE_EDGE_RATE,
            "MIN_BASELINE_WIN_RATE": MIN_BASELINE_WIN_RATE,
            "MIN_TIME_AWARE_NULL_PASS_RATE": MIN_TIME_AWARE_NULL_PASS_RATE,
            "MIN_MEDIAN_ABSOLUTE_EDGE": MIN_MEDIAN_ABSOLUTE_EDGE,
            "MIN_MEDIAN_INCREMENTAL_EDGE": MIN_MEDIAN_INCREMENTAL_EDGE,
            "MAX_TIMING_FAILURE_RATE": MAX_TIMING_FAILURE_RATE,
        },
        "windows": {str(w): _window_stats(snaps[-w:]) for w in windows},
        "note": ("Headline metrics use the primary 60s horizon only; secondary horizons stay "
                 "exploratory. A few spectacular sessions do not outweigh mostly weak or "
                 "contradictory ones — that is what the median and sign-consistency fields are for."),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Write an immutable end-of-session research snapshot.")
    ap.add_argument("symbol", nargs="?", default="SPY")
    ap.add_argument("--date", default=None, help="market date YYYY-MM-DD (default: today ET)")
    ap.add_argument("--rerun", default=None, help="reason; forces a marked rerun")
    args = ap.parse_args()
    res = take_snapshot(args.symbol, market_date=args.date,
                        force_rerun=bool(args.rerun), reason=args.rerun)
    print(json.dumps(res, indent=2))
