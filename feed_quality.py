"""
FEED QUALITY — validate a new intraday feed against the current one BEFORE any
entry logic is allowed to consume it.

The rule (per review): when you add a real intraday feed (Alpaca SIP), run it in
parallel with the existing feed (IEX) and verify the data is trustworthy before
building on it — otherwise you spend weeks validating the feed while thinking
you're validating the entry rules.

Deliberate limits encoded here, so the validator can't overclaim:
  * BAR quality and QUOTE quality are separate gates. Minute bars can validate
    OHLC, missing bars, consolidated volume, VWAP/opening-range inputs — but NOT
    NBBO spread accuracy, quote sequencing, crossed/locked markets, or adverse
    selection. Those need live quote/trade capture, so quote_quality_gate stays
    'pending_live_capture' until such events are supplied.
  * Sessions are segmented (premarket / regular / after-hours) and premarket has
    its OWN gate — a feed can be excellent in regular hours yet too thin premarket,
    and the entry policy depends on premarket VWAP/opening context.
  * validation_mode distinguishes a historical delayed comparison from a live
    parallel stream. A historical comparison can check bar construction but CANNOT
    qualify a feed for live entry use — eligible_for_level1_research is False
    unless the run is a live parallel stream that also passed the quote gate.
    (Passing means suitable for Level-1 alerts and paper research only — never
    autonomous live execution.)
  * The IEX/SIP volume gap is DESCRIPTIVE (IEX is ~2.5% of consolidated volume),
    not a hard ratio — coverage varies by symbol, time of day, and session.

Nothing here connects a feed to execution.
"""

import numpy as np

import bar_builder as bb

FEED_QUALITY_VERSION = "feed-quality-v2"

# provisional acceptance thresholds — freeze before a validation run
MAX_MEDIAN_MID_DIVERGENCE_BPS = 3.0
MAX_P99_MID_DIVERGENCE_BPS = 15.0
MIN_CLOSE_CORRELATION = 0.999
MAX_MISSING_BAR_RATE = 0.02
MIN_PREMARKET_SHARED_BARS = 30

# ET session boundaries (minutes since ET midnight)
_PREMARKET = (4 * 60, 9 * 60 + 30)      # 04:00–09:30
_REGULAR = (9 * 60 + 30, 16 * 60)       # 09:30–16:00
_AFTERHOURS = (16 * 60, 20 * 60)        # 16:00–20:00


def session_of_et_minutes(m):
    if _PREMARKET[0] <= m < _PREMARKET[1]:
        return "premarket"
    if _REGULAR[0] <= m < _REGULAR[1]:
        return "regular"
    if _AFTERHOURS[0] <= m < _AFTERHOURS[1]:
        return "afterhours"
    return "closed"


def _grid_completeness(minutes, span):
    if span <= 0:
        return None
    return round(min(1.0, len(set(int(m) for m in minutes)) / span), 4)


def _pct(a, p):
    return float(np.percentile(a, p)) if len(a) else None


def _compare_core(bars_a, bars_b):
    """Divergence / correlation / volume / missing for one aligned subset.
    bars_* : dicts with t (int minute key), close, volume."""
    by_a = {int(b["t"]): b for b in bars_a}
    by_b = {int(b["t"]): b for b in bars_b}
    all_t = sorted(set(by_a) | set(by_b))
    if not all_t:
        return None
    span = all_t[-1] - all_t[0] + 1
    shared = [t for t in all_t if t in by_a and t in by_b]
    div, ca_l, cb_l, vr = [], [], [], []
    identical_vol = 0
    for t in shared:
        ca, cb = float(by_a[t]["close"]), float(by_b[t]["close"])
        if ca > 0 and cb > 0:
            div.append(abs(ca - cb) / cb * 1e4)
            ca_l.append(ca); cb_l.append(cb)
        va = float(by_a[t].get("volume", 0) or 0)
        vb = float(by_b[t].get("volume", 0) or 0)
        if vb > 0:
            vr.append(va / vb)
        if va == vb and va > 0:
            identical_vol += 1
    corr = (float(np.corrcoef(ca_l, cb_l)[0, 1])
            if len(ca_l) > 2 and np.std(ca_l) > 0 and np.std(cb_l) > 0 else None)
    med_vr = float(np.median(vr)) if vr else None
    return {
        "shared_bars": len(shared),
        "median_divergence_bps": round(np.median(div), 3) if div else None,
        "p99_divergence_bps": round(_pct(div, 99), 3) if div else None,
        "close_correlation": round(corr, 6) if corr is not None else None,
        "new_over_ref_median_volume_ratio": round(med_vr, 3) if med_vr is not None else None,
        "volume_coverage_improved": (bool(med_vr > 1.05) if med_vr is not None else None),
        "missing_bar_rate_new": round(1 - _grid_completeness(by_a.keys(), span), 4),
        "missing_bar_rate_ref": round(1 - _grid_completeness(by_b.keys(), span), 4),
        # same-feed trap: if volumes are identical on most shared bars, the two
        # requests probably hit the SAME feed (Alpaca defaults to SIP once subscribed)
        "identical_volume_fraction": round(identical_vol / len(shared), 3) if shared else None,
    }


def compare_bar_series(bars_new, bars_ref):
    """Overall + per-ET-session comparison. bars_* carry t, close, volume, and
    'session' (premarket/regular/afterhours/closed)."""
    overall = _compare_core(bars_new, bars_ref)
    if overall is None:
        return {"available": False, "reason": "no bars from either feed"}
    by_session = {}
    for sess in ("premarket", "regular", "afterhours"):
        a = [b for b in bars_new if b.get("session") == sess]
        b = [b for b in bars_ref if b.get("session") == sess]
        core = _compare_core(a, b) if (a or b) else None
        if core:
            by_session[sess] = core
    return {"available": True, "overall": overall, "by_session": by_session}


def per_feed_timing(events, label):
    if not events:
        return {"label": label, "available": False}
    bars, quality = bb.build_bars_audited(events)
    import random
    shuffled = events[:]
    random.Random(7).shuffle(shuffled)
    bars2, _ = bb.build_bars_audited(shuffled)
    h1 = {b["bar_index"]: b["input_hash"] for b in bars}
    h2 = {b["bar_index"]: b["input_hash"] for b in bars2}
    return {"label": label, "available": True, "timing_quality": quality,
            "replay_reproducible": bool(h1 == h2)}


def _bar_gate(core):
    """PASS/FAIL for one session's bar comparison. Volume ratio is NOT gated."""
    if core is None:
        return False, ["no_bars"]
    failed = []
    if core["median_divergence_bps"] is not None and core["median_divergence_bps"] > MAX_MEDIAN_MID_DIVERGENCE_BPS:
        failed.append("median_divergence_bps")
    if core["p99_divergence_bps"] is not None and core["p99_divergence_bps"] > MAX_P99_MID_DIVERGENCE_BPS:
        failed.append("p99_divergence_bps")
    if core["close_correlation"] is not None and core["close_correlation"] < MIN_CLOSE_CORRELATION:
        failed.append("close_correlation")
    if core["missing_bar_rate_new"] > MAX_MISSING_BAR_RATE:
        failed.append("missing_bar_rate_new_feed")
    return (len(failed) == 0), failed


def feed_quality_report(bars_new, bars_ref, events_new=None, events_ref=None,
                        label_new="sip", label_ref="iex",
                        validation_mode="historical_delayed_comparison"):
    cmp = compare_bar_series(bars_new, bars_ref)
    if not cmp.get("available"):
        return {"feed_quality_version": FEED_QUALITY_VERSION, "available": False,
                "reason": cmp.get("reason")}

    reg = cmp["by_session"].get("regular")
    pre = cmp["by_session"].get("premarket")
    reg_pass, reg_failed = _bar_gate(reg)
    pre_pass, pre_failed = _bar_gate(pre)
    if pre and pre["shared_bars"] < MIN_PREMARKET_SHARED_BARS:
        pre_pass = False; pre_failed.append("insufficient_premarket_bars")

    # same-feed trap detection (default-feed accident)
    ov = cmp["overall"]
    same_feed_suspected = bool(ov.get("identical_volume_fraction") is not None
                               and ov["identical_volume_fraction"] > 0.9)

    # quote gate: only meaningful with live quote/trade events
    if events_new:
        tnew = per_feed_timing(events_new, label_new)
        quote_pass = bool(tnew["timing_quality"]["passed"] and tnew["replay_reproducible"])
        quote_gate = "passed" if quote_pass else "failed"
    else:
        tnew = {"available": False}
        quote_gate = "pending_live_capture"

    bar_quality_gate = "passed" if reg_pass else "failed"
    premarket_quality_gate = "passed" if pre_pass else ("failed" if pre else "no_premarket_data")

    live = validation_mode == "live_parallel_stream"
    eligible = bool(live and reg_pass and pre_pass and quote_gate == "passed"
                    and not same_feed_suspected)
    # Deliberately NOT "approved": passing means the feed is clean enough for
    # Level-1 candidate alerts and paper entry research — NOT for autonomous live
    # execution. "approved" tends to drift into "approved to trade" in dashboards.
    overall_status = "live_feed_quality_passed" if eligible else "provisional"

    return {
        "feed_quality_version": FEED_QUALITY_VERSION,
        "available": True,
        "new_feed": label_new, "reference_feed": label_ref,
        "validation_mode": validation_mode,
        "same_feed_suspected": same_feed_suspected,
        "bar_quality_gate": bar_quality_gate,
        "premarket_quality_gate": premarket_quality_gate,
        "quote_quality_gate": quote_gate,
        "overall_status": overall_status,
        "eligible_for_level1_research": eligible,
        "eligible_for_level1_research_means": ("clean enough for Level-1 candidate alerts and "
                                               "paper entry research only — NOT autonomous live "
                                               "execution"),
        "regular_session_failed_checks": reg_failed,
        "premarket_failed_checks": pre_failed,
        "comparison": cmp,
        "timing": {label_new: tnew},
        "thresholds": {
            "MAX_MEDIAN_MID_DIVERGENCE_BPS": MAX_MEDIAN_MID_DIVERGENCE_BPS,
            "MAX_P99_MID_DIVERGENCE_BPS": MAX_P99_MID_DIVERGENCE_BPS,
            "MIN_CLOSE_CORRELATION": MIN_CLOSE_CORRELATION,
            "MAX_MISSING_BAR_RATE": MAX_MISSING_BAR_RATE,
            "MIN_PREMARKET_SHARED_BARS": MIN_PREMARKET_SHARED_BARS,
        },
        "remaining_requirements": [
            "run as a LIVE parallel stream (not historical delayed) across >= 5 sessions",
            "capture and pass the QUOTE gate (NBBO spread, sequencing, crossed/locked)",
            "confirm explicit per-request feed selection (same_feed_suspected must be false)",
            "separate premarket pass, not compensated by regular-session quality",
            "test disconnect / replay behavior, then freeze the feed adapter version",
        ],
        "note": ("Volume ratio is DESCRIPTIVE (IEX is ~2.5% of consolidated volume; the ratio "
                 "varies by symbol/time/session) — not a required number. This gate decides only "
                 "whether a feed is clean enough for the NEXT research layer; it never connects a "
                 "feed to execution. A historical comparison can never approve a live feed."),
    }
