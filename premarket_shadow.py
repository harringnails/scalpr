"""
PREMARKET SHADOW LOGGER — observational forward test of the scorecard.

Purpose: record what the premarket scorecard concluded BEFORE the open, then
attach what actually happened AFTER the open, in two separate immutable files,
so that later we can ask honestly: does 'favorable background' skew returns
positive, does 'WAIT' avoid poor entries, do low-confidence calls perform worse.

Hard rules (mirroring the session-snapshot discipline):
  * The assessment is frozen before the outcome window. An assessment created
    after the cutoff (09:25 ET) is written but flagged ineligible — it cannot
    enter the forward test, because it would be judged against data it could
    have seen.
  * Assessment and outcomes are stored SEPARATELY and never rewritten. The
    assessment file is the proof the prediction existed before the market moved.
  * No hindsight relabeling: once written, background / blocking / confidence
    are fixed. A re-run is a marked revision and is excluded from the primary
    forward test unless it too was generated before the cutoff.
  * Nothing here influences trades, sizing, or the scorecard logic.

Pre-registered evaluation (fixed before collecting):
  PRIMARY_OUTCOME_HORIZON_MINUTES = 60
  PRIMARY_DIRECTION_METRIC        = signed_return_60m
  PRIMARY_ENTRY_METRIC            = mfe_minus_abs_mae_60m
  MIN_SHADOW_SESSIONS             = 20
Other horizons (15m, 30m, close) are diagnostics, never the headline.

Interpretation rule kept explicit: a WAIT is not 'correct' just because price
fell. It is useful when it correctly flags insufficient evidence or poor setup
quality. So the summary reports directional correctness AND decision-quality
usefulness as separate things.
"""

import csv
import hashlib
import json
import math
import os
from datetime import datetime, timezone, time as dtime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                       # pragma: no cover
    ET = timezone.utc

SHADOW_DIR = Path(__file__).parent / "premarket_shadow"
ASSESSMENT_CUTOFF_ET = dtime(9, 25)     # prediction must be frozen by here
OUTCOME_OPEN_ET = dtime(9, 30)          # outcome window starts at the official open

HORIZONS_MIN = [15, 30, 60]
PRIMARY_OUTCOME_HORIZON_MINUTES = 60
PRIMARY_DIRECTION_METRIC = "signed_return_60m"
PRIMARY_ENTRY_METRIC = "mfe_minus_abs_mae_60m"
MIN_SHADOW_SESSIONS = 20
HIGH_CONFIDENCE_THRESHOLD = 0.60        # split for confidence-conditioned accuracy
MIN_OUTCOME_COMPLETENESS = 0.90         # first-hour minute-bar coverage required to be complete

# Frozen price convention (do not change during the first 20 sessions; a change
# starts a new cohort). t = elapsed minutes = round((bar_start_ts - open_ts)/60).
PRICE_CONVENTION_VERSION = "price-convention-v1"
PRICE_CONVENTION = {
    "version": PRICE_CONVENTION_VERSION,
    "open": "open of the first session minute bar (t=0), i.e. the official 09:30 ET open",
    "h_minute_outcome": "close of the last bar with elapsed t <= h (price ~h minutes into session)",
    "close": "close of the last regular-session minute bar (official close)",
    "bar_timestamp_semantics": "bar start time; if the feed proves to use bar-end stamps the "
                               "convention shifts by one minute and would require v2",
}


# ── helpers ────────────────────────────────────────────────────────────────

def _atomic_write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path):
    if not path.name.endswith(".json") or path.name.endswith(".tmp"):
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _bg_bucket(background):
    """Collapse the granular background label into favorable / unfavorable /
    mixed for the summary."""
    if background in ("favorable", "leaning_bullish"):
        return "favorable"
    if background in ("unfavorable", "leaning_bearish"):
        return "unfavorable"
    return "mixed"


def _session_dir(symbol, market_date):
    return SHADOW_DIR / symbol.upper() / str(market_date)


# ── 1. log the assessment (before the open) ────────────────────────────────

def log_assessment(scorecard, market_date=None, now=None, cutoff=None):
    """Persist a frozen premarket assessment. `scorecard` is a run_premarket()
    result dict. Returns a small status dict. Immutable + idempotent per
    (symbol, market_date, scorecard_version)."""
    symbol = scorecard["symbol"]
    if market_date is None:
        market_date = datetime.now(ET).date()
    if isinstance(market_date, str):
        market_date = datetime.fromisoformat(market_date).date()
    now = now or datetime.now(timezone.utc)
    cutoff = cutoff or datetime.combine(market_date, ASSESSMENT_CUTOFF_ET, tzinfo=ET)

    eligible = now.astimezone(ET) <= cutoff.astimezone(ET)
    comp_status = {k: v.get("status") for k, v in scorecard.get("assessments", {}).items()}
    input_manifest = {"symbol": symbol, "market_date": str(market_date),
                      "component_status": comp_status,
                      "data_confidence": scorecard.get("data_confidence", {}).get("score")}

    record = {
        "scorecard_version": scorecard.get("scorecard_version"),
        "policy_version": scorecard.get("decision_policy", {}).get("version"),
        "symbol": symbol,
        "market_date": str(market_date),
        "assessment_created_at": now.astimezone(timezone.utc).isoformat(),
        "assessment_cutoff": cutoff.astimezone(timezone.utc).isoformat(),
        "eligible_for_shadow_evaluation": bool(eligible),
        "ineligibility_reason": None if eligible else "assessment_after_cutoff",

        "background": scorecard.get("background"),
        "readiness": scorecard.get("readiness"),
        "blocking_condition": scorecard.get("blocking_condition"),
        "data_confidence": scorecard.get("data_confidence", {}).get("score"),

        "components": scorecard.get("assessments", {}),
        "raw_scorecard_output": scorecard,
        "input_manifest_hash": _hash(input_manifest),

        "analysis_mode": "observational_shadow",
        "connected_to_execution": False,
        "is_revision": False,
    }
    record["assessment_hash"] = _hash({k: record[k] for k in
                                       ("scorecard_version", "symbol", "market_date",
                                        "background", "readiness", "blocking_condition",
                                        "data_confidence", "input_manifest_hash")})

    d = _session_dir(symbol, market_date)
    path = d / "assessment.json"
    if path.exists():
        # never overwrite a frozen assessment — write a marked revision instead
        existing = _read_json(path) or {}
        record["is_revision"] = True
        record["revises_created_at"] = existing.get("assessment_created_at")
        # a revision is eligible for the primary test ONLY if it too beat the cutoff
        n = 1
        rp = d / f"assessment_revision_{n}.json"
        while rp.exists():
            n += 1
            rp = d / f"assessment_revision_{n}.json"
        _atomic_write_json(rp, record)
        return {"logged": True, "revision": True, "path": str(rp),
                "eligible": bool(eligible)}

    _atomic_write_json(path, record)
    return {"logged": True, "revision": False, "path": str(path),
            "eligible": bool(eligible), "assessment_hash": record["assessment_hash"]}


# ── 2. outcome computation (pure) + attach (after the session) ─────────────

def compute_outcomes(session_bars, prior_day_high=None, prior_day_low=None):
    """Pure: from minute bars at/after the official open, compute the forward
    returns and excursions. `session_bars` is a list of dicts with keys
    t (minutes-from-open int), open, high, low, close, volume — already filtered
    to >= 09:30 ET and sorted."""
    if not session_bars:
        return {"available": False, "outcome_data_complete": False,
                "completeness_reason": "no_session_bars", "price_convention": PRICE_CONVENTION_VERSION}
    open_price = float(session_bars[0]["open"])
    if open_price <= 0:
        return {"available": False, "outcome_data_complete": False,
                "completeness_reason": "bad_open_price", "price_convention": PRICE_CONVENTION_VERSION}

    def at(minute):
        # last bar with t <= minute (the price `minute` minutes into the session)
        chosen = None
        for b in session_bars:
            if b["t"] <= minute:
                chosen = b
            else:
                break
        return chosen

    out = {"available": True, "open_price": round(open_price, 4)}
    for h in HORIZONS_MIN:
        b = at(h)
        out[f"return_{h}m"] = (round(math.log(float(b["close"]) / open_price), 6)
                               if b and float(b["close"]) > 0 else None)
    last = session_bars[-1]
    out["return_close"] = round(math.log(float(last["close"]) / open_price), 6) if float(last["close"]) > 0 else None

    first_hour = [b for b in session_bars if b["t"] <= 60]
    if first_hour:
        highs = [float(b["high"]) for b in first_hour]
        lows = [float(b["low"]) for b in first_hour]
        out["mfe_60m"] = round(math.log(max(highs) / open_price), 6)
        out["mae_60m"] = round(math.log(min(lows) / open_price), 6)
        # opening range = first 15 minutes
        orb = [b for b in first_hour if b["t"] <= 15]
        if orb:
            out["opening_range_high"] = round(max(float(b["high"]) for b in orb), 4)
            out["opening_range_low"] = round(min(float(b["low"]) for b in orb), 4)
        rets = [math.log(float(first_hour[i]["close"]) / float(first_hour[i - 1]["close"]))
                for i in range(1, len(first_hour))
                if float(first_hour[i - 1]["close"]) > 0]
        out["realized_vol_first_hour"] = (round(float(_std(rets)), 6) if len(rets) > 1 else None)
        out["prior_day_high_touched"] = (bool(max(highs) >= prior_day_high)
                                         if prior_day_high else None)
        out["prior_day_low_touched"] = (bool(min(lows) <= prior_day_low)
                                        if prior_day_low else None)
        # expected ~60 one-minute bars in the first hour
        out["data_completeness_first_hour"] = round(min(1.0, len(first_hour) / 60.0), 3)
    # pre-registered composite entry metric
    if out.get("mfe_60m") is not None and out.get("mae_60m") is not None:
        out["mfe_minus_abs_mae_60m"] = round(out["mfe_60m"] - abs(out["mae_60m"]), 6)

    # completeness gate — abstain rather than compute the primary metric from
    # partial data. Requires the 60m and close prices and >=90% first-hour bars.
    fh = out.get("data_completeness_first_hour")
    complete = (out.get(f"return_{PRIMARY_OUTCOME_HORIZON_MINUTES}m") is not None
                and out.get("return_close") is not None
                and fh is not None and fh >= MIN_OUTCOME_COMPLETENESS)
    out["outcome_data_complete"] = bool(complete)
    if not complete:
        out["completeness_reason"] = "incomplete_data"
    out["price_convention"] = PRICE_CONVENTION_VERSION
    return out


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def attach_outcomes(symbol, market_date, session_bars, prior_day_high=None,
                    prior_day_low=None, now=None):
    """Compute outcomes and write them as a SEPARATE immutable file next to the
    assessment. Never rewrites the assessment. Idempotent."""
    if isinstance(market_date, str):
        market_date = datetime.fromisoformat(market_date).date()
    now = now or datetime.now(timezone.utc)
    d = _session_dir(symbol, market_date)
    if not (d / "assessment.json").exists():
        return {"attached": False, "reason": "no assessment for this session"}
    op = d / "outcomes.json"
    if op.exists():
        return {"attached": False, "reason": "outcomes already recorded", "path": str(op)}
    outcomes = compute_outcomes(session_bars, prior_day_high, prior_day_low)
    record = {
        "symbol": symbol.upper(), "market_date": str(market_date),
        "outcomes_recorded_at": now.astimezone(timezone.utc).isoformat(),
        "outcome_open_et": OUTCOME_OPEN_ET.isoformat(),
        "primary_outcome_horizon_minutes": PRIMARY_OUTCOME_HORIZON_MINUTES,
        "price_convention": PRICE_CONVENTION,
        "outcome_data_complete": bool(outcomes.get("outcome_data_complete")),
        "outcomes": outcomes,
        "analysis_mode": "observational_shadow",
    }
    _atomic_write_json(op, record)
    return {"attached": True, "path": str(op),
            "outcome_data_complete": record["outcome_data_complete"]}


# ── 3. read-only joined view + summary ─────────────────────────────────────

def _primary_eligibility(a, o):
    """The three flags the review asked to keep visibly distinct."""
    assessment_eligible = bool(a and a.get("eligible_for_shadow_evaluation")
                               and not a.get("is_revision"))
    outcome_complete = bool(o and o.get("outcome_data_complete"))
    return {
        "assessment_eligible": assessment_eligible,
        "outcome_data_complete": outcome_complete,
        "eligible_for_primary_evaluation": assessment_eligible and outcome_complete,
    }


def joined_view(symbol, market_date):
    d = _session_dir(symbol, market_date)
    a = _read_json(d / "assessment.json")
    o = _read_json(d / "outcomes.json")
    if not a:
        return None
    return {"assessment": a, "outcomes": o, "eligibility": _primary_eligibility(a, o)}


def _load_eligible_pairs(symbol):
    """(assessment, outcomes) pairs that pass ALL three gates: assessment frozen
    pre-cutoff, not a revision, AND outcome data complete. An eligible morning
    prediction with incomplete afternoon data does NOT enter the primary sample."""
    base = SHADOW_DIR / symbol.upper()
    if not base.exists():
        return []
    pairs = []
    for date_dir in sorted(base.iterdir()):
        if not date_dir.is_dir():
            continue
        a = _read_json(date_dir / "assessment.json")
        o = _read_json(date_dir / "outcomes.json")
        if not _primary_eligibility(a, o)["eligible_for_primary_evaluation"]:
            continue
        pairs.append((a, o))
    return pairs


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def shadow_summary(symbol="SPY"):
    """Read-only descriptive summary over eligible sessions. Reports directional
    correctness AND decision-quality usefulness as separate things — a WAIT is
    not 'right' just because price fell."""
    pairs = _load_eligible_pairs(symbol)
    n = len(pairs)
    prim = f"return_{PRIMARY_OUTCOME_HORIZON_MINUTES}m"

    # count morning-eligible predictions dropped for incomplete afternoon data
    dropped_incomplete = 0
    base = SHADOW_DIR / symbol.upper()
    if base.exists():
        for date_dir in base.iterdir():
            if not date_dir.is_dir():
                continue
            a = _read_json(date_dir / "assessment.json")
            o = _read_json(date_dir / "outcomes.json")
            e = _primary_eligibility(a, o)
            if e["assessment_eligible"] and not e["outcome_data_complete"]:
                dropped_incomplete += 1

    def ret(o):
        return o["outcomes"].get(prim)

    buckets = {"favorable": [], "unfavorable": [], "mixed": []}
    for a, o in pairs:
        buckets[_bg_bucket(a.get("background"))].append((a, o))

    # directional correctness: favorable -> +ret is correct; unfavorable -> -ret
    def dir_correct(a, o):
        r = ret(o)
        if r is None:
            return None
        b = _bg_bucket(a.get("background"))
        if b == "favorable":
            return r > 0
        if b == "unfavorable":
            return r < 0
        return None   # mixed makes no directional claim

    graded = [(a, o, dir_correct(a, o)) for a, o in pairs]
    directional = [g for g in graded if g[2] is not None]
    hi = [g for g in directional if (g[0].get("data_confidence") or 0) >= HIGH_CONFIDENCE_THRESHOLD]
    lo = [g for g in directional if (g[0].get("data_confidence") or 0) < HIGH_CONFIDENCE_THRESHOLD]

    def acc(group):
        return round(sum(1 for g in group if g[2]) / len(group), 3) if group else None

    wait = [(a, o) for a, o in pairs if str(a.get("readiness", "")).find("wait") >= 0]
    wait_mae = [o["outcomes"].get("mae_60m") for _, o in wait]
    nonwait_mae = [o["outcomes"].get("mae_60m") for a, o in pairs
                   if str(a.get("readiness", "")).find("wait") < 0]

    result = {
        "symbol": symbol.upper(),
        "analysis_mode": "observational_shadow",
        "scorecard_version": pairs[0][0].get("scorecard_version") if pairs else None,
        "preregistered": {
            "PRIMARY_OUTCOME_HORIZON_MINUTES": PRIMARY_OUTCOME_HORIZON_MINUTES,
            "PRIMARY_DIRECTION_METRIC": PRIMARY_DIRECTION_METRIC,
            "PRIMARY_ENTRY_METRIC": PRIMARY_ENTRY_METRIC,
            "MIN_SHADOW_SESSIONS": MIN_SHADOW_SESSIONS,
        },
        "eligible_sessions": n,
        "assessment_eligible_but_incomplete_outcome": dropped_incomplete,
        "review_stage": ("formal_shadow_evaluation" if n >= MIN_SHADOW_SESSIONS
                         else "preliminary_descriptive" if n >= 10
                         else "operational_quality" if n >= 5
                         else "insufficient"),
        "favorable_background_sessions": len(buckets["favorable"]),
        "unfavorable_background_sessions": len(buckets["unfavorable"]),
        "mixed_background_sessions": len(buckets["mixed"]),
        "favorable_median_return_60m": _round(_median([ret(o) for _, o in buckets["favorable"]])),
        "unfavorable_median_return_60m": _round(_median([ret(o) for _, o in buckets["unfavorable"]])),
        "mixed_median_return_60m": _round(_median([ret(o) for _, o in buckets["mixed"]])),

        # directional correctness (did the background predict the sign)
        "directional_correctness": {
            "graded_sessions": len(directional),
            "overall_accuracy": acc(directional),
            "high_confidence_accuracy": acc(hi),
            "low_confidence_accuracy": acc(lo),
        },
        # decision-quality usefulness (kept SEPARATE from directional correctness)
        "decision_quality": {
            "wait_sessions": len(wait),
            "wait_median_mae_60m": _round(_median(wait_mae)),
            "non_wait_median_mae_60m": _round(_median(nonwait_mae)),
            "interpretation": ("A WAIT is useful if it avoided poor entries — compare "
                               "wait_median_mae_60m against non_wait_median_mae_60m — NOT merely "
                               "whether price fell."),
        },
        "note": ("Descriptive only over eligible (pre-cutoff, non-revision) sessions with recorded "
                 "outcomes. 60-minute horizon is primary; 15m/30m/close are diagnostics. Do not "
                 "tune the scorecard from fewer than 20 sessions."),
    }
    return result


def _round(v, nd=6):
    return None if v is None else round(v, nd)
