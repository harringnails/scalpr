"""
flow_evidence.py — Options-Flow Evidence ranking (`flow-evidence-v0`).

Read-only, pure. Turns a UW workup payload into a per-ticker directional-flow
READ with a 🟢 green / 🟠 amber / insufficient tier. This is EVIDENCE, not a
probability, not a buy signal, not a calibrated score (GREEN DATA ≠ GREEN TRADE).

Discipline (same as the rest of the system):
  * No probability, no edge claim. `is_edge_claim = False`.
  * Nothing fabricated — a signal with no data is `unavailable`, never inferred.
  * Options flow ONLY. Equity L2/order-flow, internals, news, true volume profile
    are NOT on the feed and are listed as unavailable.
  * The tier fuses DATA QUALITY × directional AGREEMENT of AVAILABLE signals —
    never a forecast of profit.
"""
from datetime import datetime, timezone

import feature_engine as fe

FLOW_EVIDENCE_VERSION = "flow-evidence-v0"

MIN_BAND_CONTRACTS = 5          # need at least this many real-quote in-band contracts
STALE_WORKUP_SEC = 1800         # workup older than 30 min → degraded freshness
AGGRESSIVE_PCT_HI = 55.0        # ask% above this = buyers lifting offers (per side)
MIN_AGREE_SIGNALS = 3           # green needs >= this many available signals agreeing


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _age_sec(as_of, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        t = datetime.fromisoformat(str(as_of))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (now - t).total_seconds()
    except Exception:
        return None


def score_ticker(payload, now=None):
    """Directional options-flow evidence for one ticker from its workup payload.
    Returns component signals (each with data-status), an overall direction, and
    a green/amber/insufficient tier. Pure."""
    t = (payload or {}).get("ticker")
    contracts = (payload or {}).get("contracts") or []
    band = [c for c in fe.in_band_contracts(contracts)
            if c.get("bid") and c.get("ask") and c.get("volume")]
    age = _age_sec(payload.get("as_of"), now) if payload else None
    fresh = (age is not None and age <= STALE_WORKUP_SEC)

    calls = [c for c in band if c.get("type") == "C"]
    puts = [c for c in band if c.get("type") == "P"]

    def _side_agg(rows, key):
        vals = [(_num(c.get(key)), _num(c.get("volume"))) for c in rows]
        num = sum((v * w) for v, w in vals if v is not None and w)
        den = sum(w for v, w in vals if v is not None and w)
        return (num / den) if den else None

    def _sum(rows, key):
        s = sum(_num(c.get(key)) or 0 for c in rows)
        return s

    signals = []      # each: {name, available, direction, note}

    def sig(name, available, direction=None, note=None):
        signals.append({"name": name, "available": available,
                        "direction": direction, "note": note})

    # 1. aggressive buying (ask% weighted) — calls vs puts
    if band:
        call_ask = _side_agg(calls, "ask_pct")
        put_ask = _side_agg(puts, "ask_pct")
        d = None
        if call_ask is not None or put_ask is not None:
            if (call_ask or 0) >= AGGRESSIVE_PCT_HI and (call_ask or 0) > (put_ask or 0):
                d = "BULLISH"
            elif (put_ask or 0) >= AGGRESSIVE_PCT_HI and (put_ask or 0) > (call_ask or 0):
                d = "BEARISH"
            else:
                d = "MIXED"
        sig("aggressive_buying", d is not None, d,
            f"call ask% {round(call_ask,1) if call_ask else None} / put ask% {round(put_ask,1) if put_ask else None}")
    else:
        sig("aggressive_buying", False)

    # 2. net aggressor (ask-bid vol) calls vs puts
    if band:
        cna, pna = _sum(calls, "net_aggressor"), _sum(puts, "net_aggressor")
        d = "BULLISH" if cna - pna > 0 else ("BEARISH" if cna - pna < 0 else "MIXED")
        sig("net_aggressor", True, d, f"calls {int(cna)} / puts {int(pna)}")
    else:
        sig("net_aggressor", False)

    # 3. sweeps (urgency) calls vs puts
    if band:
        cs, ps = _sum(calls, "sweep_vol"), _sum(puts, "sweep_vol")
        d = "BULLISH" if cs > ps else ("BEARISH" if ps > cs else "MIXED")
        sig("sweeps", (cs + ps) > 0, d, f"calls {int(cs)} / puts {int(ps)}")
    else:
        sig("sweeps", False)

    # 4. OI build (new positioning) calls vs puts — prefer oi_since_prev, else oi_chg
    if band:
        key = "oi_since_prev" if any(c.get("oi_since_prev") is not None for c in band) else "oi_chg"
        cb = sum(_num(c.get(key)) or 0 for c in calls)
        pb = sum(_num(c.get(key)) or 0 for c in puts)
        d = "BULLISH" if cb > pb else ("BEARISH" if pb > cb else "MIXED")
        sig("oi_build", True, d, f"[{key}] calls {int(cb)} / puts {int(pb)}")
    else:
        sig("oi_build", False)

    # 5. put/call skew by OI (context/tilt)
    if band:
        coi = sum(int(c.get("oi") or 0) for c in calls)
        poi = sum(int(c.get("oi") or 0) for c in puts)
        pcr = round(poi / coi, 3) if coi else None
        d = ("BEARISH" if (pcr or 0) > 1.3 else ("BULLISH" if pcr is not None and pcr < 0.7 else "MIXED"))
        sig("put_call_skew", pcr is not None, d, f"put/call OI {pcr}")
    else:
        sig("put_call_skew", False)

    # 6. IV rank (context only — not directional)
    ivr = payload.get("iv_rank")
    ivr_val = None
    if isinstance(ivr, dict):
        ivr_val = _num(ivr.get("iv_rank_1y"))
    elif isinstance(ivr, (int, float)):
        ivr_val = float(ivr)
    sig("iv_rank_context", ivr_val is not None, None, f"iv_rank {ivr_val}")

    # 7/8. dealer GEX + dark pool (un-discarded; context — presence only in v0)
    sig("dealer_gex", payload.get("gex_levels") is not None, None,
        "gamma-wall context available" if payload.get("gex_levels") is not None else None)
    sig("dark_pool", payload.get("darkpool") is not None, None,
        "off-exchange levels available" if payload.get("darkpool") is not None else None)

    # explicitly-unavailable tiers (never fabricated)
    unavailable = ["equity_level2_order_flow", "market_internals", "news_nlp", "true_volume_profile"]

    # ── direction + tier (data quality × agreement) ──
    directional = [s for s in signals if s["available"] and s["direction"] in ("BULLISH", "BEARISH")]
    bull = sum(1 for s in directional if s["direction"] == "BULLISH")
    bear = sum(1 for s in directional if s["direction"] == "BEARISH")
    if bull > bear:
        direction = "BULLISH"
    elif bear > bull:
        direction = "BEARISH"
    else:
        direction = "MIXED"
    agree = max(bull, bear)
    opposing = min(bull, bear)     # any opposing directional signal breaks "agreement"

    enough_data = len(band) >= MIN_BAND_CONTRACTS and fresh
    if not enough_data or not directional:
        tier, star = "INSUFFICIENT", ""
    elif direction != "MIXED" and agree >= MIN_AGREE_SIGNALS and opposing == 0:
        tier, star = "GREEN", "🟢"     # unanimous among available directional signals
    else:
        tier, star = "AMBER", "🟠"     # present but conflicting / partial

    return {
        "ticker": t, "flow_evidence_version": FLOW_EVIDENCE_VERSION,
        "is_edge_claim": False, "kind": "options_flow_evidence_only",
        "as_of": payload.get("as_of"), "workup_age_sec": (round(age, 1) if age else None),
        "fresh": fresh, "band_contracts": len(band),
        "direction": direction, "agreeing_signals": agree,
        "tier": tier, "star": star,
        "signals": signals, "unavailable_inputs": unavailable,
        "note": ("Options-flow evidence + data quality. NOT a probability, edge, or "
                 "buy signal. Equity order flow / internals / news are not on the feed."),
    }


def rank(payloads, now=None):
    """Rank tickers: GREEN → AMBER → INSUFFICIENT, then by agreeing_signals,
    then freshness. Read-only."""
    scored = [score_ticker(p, now) for p in payloads if p and p.get("ticker")]
    order = {"GREEN": 0, "AMBER": 1, "INSUFFICIENT": 2}
    scored.sort(key=lambda s: (order.get(s["tier"], 3), -s["agreeing_signals"],
                               s.get("workup_age_sec") or 1e9))
    return {"flow_evidence_version": FLOW_EVIDENCE_VERSION, "is_edge_claim": False,
            "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
            "ranking": scored,
            "legend": {"🟢": "fresh data + available signals agree on one direction",
                       "🟠": "data present but mixed/partial/degraded",
                       "INSUFFICIENT": "too little live data — shown, not guessed"},
            "note": "Evidence only. GREEN DATA ≠ GREEN TRADE."}


def rank_from_runs(runs_dir="runs", now=None):
    """Load the latest cached workup per ticker from runs/ and rank them."""
    import glob
    import json
    import os
    latest = {}
    for p in sorted(glob.glob(os.path.join(runs_dir, "*.json"))):
        base = os.path.basename(p)
        tkr = base.rsplit("_", 1)[0]
        latest[tkr] = p            # sorted → last (newest date) wins per ticker
    payloads = []
    for p in latest.values():
        try:
            payloads.append(json.load(open(p)))
        except Exception:
            continue
    return rank(payloads, now)
