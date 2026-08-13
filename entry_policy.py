"""
ENTRY POLICY — EXPLORATORY PROTOTYPE (`entry-policy-exploratory-v0`).

This is NOT the frozen `entry-policy-v1`. It exists to shake out the rule logic,
candidate lifecycle, trigger construction, and trade geometry BEFORE the SIP feed
is qualified — running on the degraded IEX feed, where it will honestly abstain
(WAIT / NO_TRADE) most of the time. That is the point: the engine refuses to
pretend the evidence is production-grade.

Hard guarantees:
  * `formal_cohort_eligible` is permanently False here. No output of this
    prototype may count toward the Phase-3 `entry-policy-v1` cohort.
  * `formal_readiness` is always NO_TRADE — no qualified policy exists pre-SIP.
  * It never places an order, never sizes, never exits. It only emits a read:
    LONG_CANDIDATE / SHORT_CANDIDATE / WAIT / NO_TRADE, with the reasons.

Design (per review): explicit HARD VETOES + required confirmations + explicit
intraday trigger + explicit trade geometry. Deliberately NOT a single numeric
score, because a score can let weak-bullish inputs paper over stale data or a bad
spread. Missing inputs on IEX are recorded as `missing_inputs` and block a FORMAL
candidate; they never get silently inferred as "passed".

After SIP passes: review only correctness/operability findings here, freeze the
actual rules as `entry-policy-v1`, and start a NEW forward-only cohort from zero.
"""

import csv
import json
import time
from datetime import datetime, timedelta, timezone

POLICY_VERSION = "entry-policy-exploratory-v0"
FORMAL_COHORT_ELIGIBLE = False           # permanent until SIP qualifies + v1 is frozen
LOG_PATH = "entry_policy_prototype_v0_log.jsonl"

# Exploratory thresholds — will be RE-SET when the real v1 is frozen. Not tuned
# from any results.
MIN_DATA_CONFIDENCE = 0.50
MIN_REWARD_RISK = 1.5
MIN_RELATIVE_VOLUME = 1.0
MAX_ATR_EXTENSION = 1.0                   # too far from VWAP in ATR = extended
OPENING_RANGE_MINUTES = 15
CANDIDATE_COOLDOWN_SEC = 300              # per (symbol, direction) — see uniqueness rule
_ASSESS_CACHE_SEC = 5
_cache = {}
_last_candidate = {}                     # (symbol, direction) -> ts


# ── intraday session data (VWAP, opening range, relative volume) ──────────

def _et_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc)


def _feed_enum(feed):
    from alpaca.data.enums import DataFeed
    return DataFeed.SIP if str(feed).lower() == "sip" else DataFeed.IEX


def _session_bars(data_client, symbol, feed="iex"):
    """Regular-session (>=09:30 ET) minute bars for today, as
    [{t_min, open, high, low, close, volume}]. On IEX these are thin — that's
    surfaced downstream, not hidden."""
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        now_et = datetime.now(ET)
        open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        if now_et < open_et:
            return [], open_et
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                               start=open_et.astimezone(timezone.utc),
                               feed=_feed_enum(feed))
        raw = data_client.get_stock_bars(req).data.get(symbol) or []
        bars = []
        for b in raw:
            t = int((b.timestamp - open_et.astimezone(timezone.utc)).total_seconds() // 60)
            if t < 0:
                continue
            bars.append({"t": t, "open": float(b.open), "high": float(b.high),
                         "low": float(b.low), "close": float(b.close),
                         "volume": float(getattr(b, "volume", 0) or 0)})
        bars.sort(key=lambda x: x["t"])
        return bars, open_et
    except Exception:
        return [], None


def _vwap(bars):
    num = den = 0.0
    series = []
    for b in bars:
        tp = (b["high"] + b["low"] + b["close"]) / 3
        num += tp * b["volume"]; den += b["volume"]
        series.append(num / den if den else b["close"])
    return series


def _intraday_context(data_client, symbol, feed="iex"):
    bars, open_et = _session_bars(data_client, symbol, feed)
    ctx = {"minutes_elapsed": len(bars), "open_et": open_et.isoformat() if open_et else None,
           "or_complete": False, "or_high": None, "or_low": None,
           "vwap": None, "vwap_slope_pos": None, "last": None,
           "above_vwap": None, "or_broken_up": None, "or_broken_dn": None,
           "held_above": None, "held_below": None, "relative_volume": None,
           "atr_extension": None, "bars_present": len(bars)}
    if not bars:
        return ctx
    ctx["last"] = bars[-1]["close"]
    orb = [b for b in bars if b["t"] < OPENING_RANGE_MINUTES]
    if len(orb) >= OPENING_RANGE_MINUTES:      # opening range fully formed
        ctx["or_complete"] = True
        ctx["or_high"] = max(b["high"] for b in orb)
        ctx["or_low"] = min(b["low"] for b in orb)
    vw = _vwap(bars)
    ctx["vwap"] = vw[-1]
    ctx["vwap_slope_pos"] = vw[-1] > vw[max(0, len(vw) - 6)]
    ctx["above_vwap"] = ctx["last"] > vw[-1]
    if ctx["or_complete"]:
        ctx["or_broken_up"] = ctx["last"] > ctx["or_high"]
        ctx["or_broken_dn"] = ctx["last"] < ctx["or_low"]
        ctx["held_above"] = sum(1 for b in bars if b["t"] >= OPENING_RANGE_MINUTES and b["close"] > ctx["or_high"]) >= 1
        ctx["held_below"] = sum(1 for b in bars if b["t"] >= OPENING_RANGE_MINUTES and b["close"] < ctx["or_low"]) >= 1
    return ctx


def _nbbo_context(data_client, symbol, feed="iex", *, now=None, stale_seconds=30):
    """Timestamp-audited underlying NBBO context; never substitutes last/mid."""
    try:
        from alpaca.data.requests import StockLatestQuoteRequest
        quote = data_client.get_stock_latest_quote(StockLatestQuoteRequest(
            symbol_or_symbols=[symbol], feed=_feed_enum(feed))).get(symbol)
        if quote is None or getattr(quote, "timestamp", None) is None:
            return {"status": "unavailable", "reason": "quote_or_timestamp_missing"}
        observed = quote.timestamp
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        received = now or datetime.now(timezone.utc)
        age = (received - observed).total_seconds()
        bid, ask = float(quote.bid_price or 0), float(quote.ask_price or 0)
        if age > stale_seconds:
            return {"status": "stale", "age_seconds": round(age, 3),
                    "provider_ts": observed.isoformat()}
        if bid <= 0 or ask <= 0 or ask < bid:
            return {"status": "unavailable", "reason": "two_sided_quote_unavailable",
                    "provider_ts": observed.isoformat()}
        mid = (bid + ask) / 2
        return {"status": "available", "bid": bid, "ask": ask,
                "spread": round(ask - bid, 6),
                "spread_bps": round((ask - bid) / mid * 10000, 3),
                "age_seconds": round(age, 3), "provider_ts": observed.isoformat()}
    except Exception:
        return {"status": "unavailable", "reason": "quote_request_failed"}


# ── the rule engine ────────────────────────────────────────────────────────

def _geometry(direction, last, vwap, or_high, or_low, atr, pdh, pdl):
    """Explicit entry / invalidation / target / reward:risk. Returns None if it
    can't be computed (missing levels/ATR)."""
    if last is None or atr is None or atr <= 0:
        return None
    if direction == "long":
        supports = [x for x in (vwap, or_low, pdl) if x is not None and x < last]
        if not supports:
            return None
        invalidation = max(supports)                 # nearest support below
        risk = last - invalidation
        resistance = pdh if (pdh and pdh > last) else last + atr
        target = min(last + MIN_REWARD_RISK * risk, resistance)
        reward = target - last
    else:  # short
        resistances = [x for x in (vwap, or_high, pdh) if x is not None and x > last]
        if not resistances:
            return None
        invalidation = min(resistances)
        risk = invalidation - last
        support = pdl if (pdl and pdl < last) else last - atr
        target = max(last - MIN_REWARD_RISK * risk, support)
        reward = last - target
    if risk <= 0:
        return None
    rr = reward / risk
    return {"entry": round(last, 4), "invalidation": round(invalidation, 4),
            "target": round(target, 4), "risk_per_unit": round(risk, 4),
            "reward_per_unit": round(reward, 4), "reward_risk": round(rr, 2),
            "atr": round(atr, 4)}


def assess_entry(data_client, symbol="SPY", feed="iex"):
    key = symbol.upper()
    feed = "sip" if str(feed).lower() == "sip" else "iex"
    cache_key = (key, feed)
    hit = _cache.get(cache_key)
    if hit and time.time() - hit[0] < _ASSESS_CACHE_SEC:
        return hit[1]

    import premarket as pm
    hard_vetoes, missing_inputs = [], []

    # --- context: premarket scorecard (background, confidence, tradability, event) ---
    try:
        sc = pm.run_premarket(data_client, key)
    except Exception as e:
        sc = None
    data_conf = (sc or {}).get("data_confidence", {}).get("score", 0.0)
    background = (sc or {}).get("background", "mixed")
    a = (sc or {}).get("assessments", {})
    tradability_status = a.get("tradability", {}).get("status")
    event_assess = a.get("event_risk", {}).get("assessment")
    event_status = a.get("event_risk", {}).get("status")

    # --- optional HMM regime gate (usually unavailable early) ---
    try:
        import regime_model as rm
        reg = rm.regime_read(key)
    except Exception:
        reg = {"available": False}
    if not reg.get("available"):
        missing_inputs.append("hmm_regime")
    elif reg.get("state") == "high_vol_disorder":
        hard_vetoes.append("unstable_regime")

    # --- degraded-feed inputs: never inferred as passed ---
    if tradability_status != "available":
        missing_inputs.append("tradability")           # blocks FORMAL, not exploratory
    if event_status != "available":
        missing_inputs.append("event_risk_calendar")
    nbbo = _nbbo_context(data_client, key, feed)
    if nbbo.get("status") != "available":
        missing_inputs.append("nbbo_spread")
    # Relative-volume qualification is not implemented for either feed yet.
    # Keep it explicitly missing rather than inferring a pass from SIP access.
    missing_inputs.append("relative_volume")

    # --- hard vetoes that stop even the EXPLORATORY read ---
    if data_conf < MIN_DATA_CONFIDENCE:
        hard_vetoes.append("insufficient_data_confidence")
    if event_assess == "elevated":
        hard_vetoes.append("event_risk")

    # --- direction from background ---
    if background in ("favorable", "leaning_bullish"):
        direction = "long"
    elif background in ("unfavorable", "leaning_bearish"):
        direction = "short"
    else:
        direction = None

    # --- intraday context + geometry ---
    ic = _intraday_context(data_client, key, feed)
    geom = None
    if direction:
        try:
            ohlc, ost = pm._ohlc_status(data_client, key)
            atr = pm._true_atr(ohlc) if ohlc else None
            pdh = ohlc[-1]["h"] if ohlc else None
            pdl = ohlc[-1]["l"] if ohlc else None
        except Exception:
            atr = pdh = pdl = None
        geom = _geometry(direction, ic["last"], ic["vwap"], ic["or_high"],
                         ic["or_low"], atr, pdh, pdl)
        if geom and geom["reward_risk"] < MIN_REWARD_RISK:
            hard_vetoes.append("poor_trade_geometry")

    # --- decide (exploratory) ---
    trigger_state = {k: ic[k] for k in ("or_complete", "above_vwap", "vwap_slope_pos",
                     "or_broken_up", "or_broken_dn", "held_above", "held_below",
                     "atr_extension", "minutes_elapsed")}
    if hard_vetoes:
        decision, reason = "NO_TRADE", hard_vetoes[0]
    elif direction is None:
        decision, reason = "NO_TRADE", "background_not_supportive"
    elif geom is None:
        decision, reason = "WAIT", "waiting_for_intraday_levels"
    elif not ic["or_complete"]:
        decision, reason = "WAIT", "waiting_for_opening_range"
    else:
        if direction == "long":
            trig = (ic["above_vwap"] and ic["vwap_slope_pos"] and ic["or_broken_up"]
                    and ic["held_above"])
        else:
            trig = ((ic["above_vwap"] is False) and (ic["vwap_slope_pos"] is False)
                    and ic["or_broken_dn"] and ic["held_below"])
        if trig:
            decision = "LONG_CANDIDATE" if direction == "long" else "SHORT_CANDIDATE"
            reason = "exploratory_trigger_complete"
        else:
            decision, reason = "WAIT", "waiting_for_entry_confirmation"

    result = {
        "policy_version": POLICY_VERSION,
        "formal_cohort_eligible": FORMAL_COHORT_ELIGIBLE,   # permanently False
        "feed_status": ("sip_observed_unqualified" if feed == "sip" else "degraded_iex"),
        "symbol": key,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "price": ic["last"],                  # underlying price at decision time
        "session_minute": ic["minutes_elapsed"],
        "decision": decision,                 # EXPLORATORY read only
        "direction": direction,
        "reason": reason,
        "hard_vetoes": hard_vetoes,
        "missing_inputs": sorted(set(missing_inputs)),
        "trigger_state": trigger_state,
        "trade_geometry": geom,
        "background": background,
        "data_confidence": round(data_conf, 3),
        "nbbo": nbbo,
        # SIP observation is now available, but the FORMAL policy still has not
        # been frozen and qualified out-of-sample — always NO_TRADE.
        "formal_readiness": "NO_TRADE",
        "formal_readiness_reason": "formal_policy_not_frozen_or_qualified",
        "disclaimer": (("Exploratory prototype on observed SIP data. " if feed == "sip" else
                        "Exploratory prototype on the degraded IEX feed. ") +
                       "NOT the frozen "
                       "entry-policy-v1. No output counts toward the Phase-3 cohort; no orders "
                       "are ever placed. A candidate here means 'the rule logic fired', not "
                       "'an approved trade'."),
    }
    _cache[cache_key] = (time.time(), result)
    return result


# ── decision logging (evenly sampled; drives outcomes + abstention scoring) ─
# The server loop calls assess_entry once/minute during market hours and logs
# the result here — so WAIT/NO_TRADE are recorded too, not only fired candidates.
# assess_entry itself is a pure read with no logging side effect.

DECISIONS_LOG = "entry_policy_prototype_v0_decisions.jsonl"
OUTCOMES_LOG = "entry_policy_prototype_v0_outcomes.jsonl"
COST_BPS = 1.0   # placeholder modeled round-trip cost, in bps of notional (labeled)


def log_decision(result, market_date):
    """Append one decision sample. decision_id is minute-granular so a double
    fire in the same minute de-dupes. Only logs when an intraday price exists
    (i.e. market open)."""
    if result.get("price") is None:
        return None
    from pathlib import Path
    dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    decision_id = f"{result['symbol']}:{market_date}:{dt.strftime('%H%M')}"
    # de-dupe by id (in case the loop double-fires within a minute)
    p = Path(DECISIONS_LOG)
    if p.exists():
        with p.open() as f:
            for line in f:
                if f'"decision_id": "{decision_id}"' in line:
                    return decision_id
    is_new_candidate = False
    if result["decision"] in ("LONG_CANDIDATE", "SHORT_CANDIDATE"):
        k = (result["symbol"], result["direction"])
        if time.time() - _last_candidate.get(k, 0) >= CANDIDATE_COOLDOWN_SEC:
            _last_candidate[k] = time.time(); is_new_candidate = True
    rec = {"decision_id": decision_id, "market_date": str(market_date),
           "decision_time": result["as_of"], "symbol": result["symbol"],
           "session_minute": result["session_minute"], "price": result["price"],
           "decision": result["decision"], "direction": result["direction"],
           "reason": result["reason"], "hard_vetoes": result["hard_vetoes"],
           "missing_inputs": result["missing_inputs"], "data_confidence": result["data_confidence"],
           "trade_geometry": result["trade_geometry"], "is_new_candidate": is_new_candidate,
           "formal_cohort_eligible": FORMAL_COHORT_ELIGIBLE}
    try:
        with open(DECISIONS_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return decision_id


def _read_jsonl(path, symbol=None, market_date=None):
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with p.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if symbol and r.get("symbol") != symbol.upper():
                continue
            if market_date and str(r.get("market_date")) != str(market_date):
                continue
            out.append(r)
    return out


def recent_candidates(symbol="SPY", limit=50):
    return [d for d in _read_jsonl(DECISIONS_LOG, symbol)
            if d.get("decision") in ("LONG_CANDIDATE", "SHORT_CANDIDATE")][-limit:]


def market_closed_assessment(symbol="SPY", feed="iex"):
    """Fast, explicit abstention outside an exchange session.

    The latest recorded decision is context only. It is never relabeled as a
    current assessment and no Alpaca market-data request is made here.
    """
    key = str(symbol).upper()
    feed = "sip" if str(feed).lower() == "sip" else "iex"
    prior = _read_jsonl(DECISIONS_LOG, key)
    latest = prior[-1] if prior else None
    return {
        "policy_version": POLICY_VERSION,
        "formal_cohort_eligible": FORMAL_COHORT_ELIGIBLE,
        "feed_status": f"{feed}_market_closed",
        "symbol": key,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "price": None,
        "session_minute": None,
        "decision": "NO_TRADE",
        "direction": None,
        "reason": "market_closed",
        "hard_vetoes": ["market_closed"],
        "missing_inputs": ["live_market_session"],
        "trigger_state": {},
        "trade_geometry": None,
        "background": "unavailable",
        "data_confidence": 0.0,
        "nbbo": {"status": "unavailable", "reason": "market_closed"},
        "formal_readiness": "NO_TRADE",
        "formal_readiness_reason": "market_closed",
        "latest_recorded_decision": ({
            "decision_id": latest.get("decision_id"),
            "decision_time": latest.get("decision_time"),
            "decision": latest.get("decision"),
            "direction": latest.get("direction"),
        } if latest else None),
        "disclaimer": ("The market is closed. This is an explicit abstention, not "
                       "a fresh entry assessment; the prior decision is context only."),
    }


# ── hypothetical outcome attachment (pure; no future-data leak) ────────────

_HORIZONS_MIN = [5, 15, 30, 60]


def _at(bars, minute):
    chosen = None
    for b in bars:
        if b["t"] <= minute:
            chosen = b
        else:
            break
    return chosen


def compute_decision_outcome(rec, session_bars):
    """Hypothetical forward outcome for one decision. Uses ONLY bars strictly
    after the decision minute (no future-data leak: the decision is fixed at its
    price, outcomes come from what happened next). Scores candidates (stop/target
    ordering + geometry PnL) AND WAIT/NO_TRADE (forward returns only), so
    abstention can be evaluated too."""
    import math
    sm = rec.get("session_minute")
    entry = rec.get("price")
    if sm is None or entry is None or not session_bars:
        return {"available": False, "reason": "insufficient_data"}
    future = [b for b in session_bars if b["t"] > sm]
    if not future:
        return {"available": False, "reason": "no_forward_bars"}

    out = {"available": True}
    for h in _HORIZONS_MIN:
        b = _at(future, sm + h)
        out[f"return_{h}m"] = (round(math.log(b["close"] / entry), 6)
                               if b and entry > 0 and b["close"] > 0 else None)
    fh = [b for b in future if b["t"] <= sm + 60]
    if fh:
        out["mfe"] = round(max(math.log(b["high"] / entry) for b in fh), 6)
        out["mae"] = round(min(math.log(b["low"] / entry) for b in fh), 6)

    # candidate-only: stop/target ordering + placeholder PnL
    g = rec.get("trade_geometry")
    if rec.get("decision") in ("LONG_CANDIDATE", "SHORT_CANDIDATE") and g:
        long = rec["direction"] == "long"
        stop, tgt = g["invalidation"], g["target"]
        first_hit, mins = None, None
        for b in fh:
            hit_stop = (b["low"] <= stop) if long else (b["high"] >= stop)
            hit_tgt = (b["high"] >= tgt) if long else (b["low"] <= tgt)
            if hit_stop and hit_tgt:
                first_hit, mins = "ambiguous_same_bar", b["t"] - sm; break
            if hit_stop:
                first_hit, mins = "stop", b["t"] - sm; break
            if hit_tgt:
                first_hit, mins = "target", b["t"] - sm; break
        exit_px = (tgt if first_hit == "target" else stop if first_hit == "stop"
                   else fh[-1]["close"])
        gross = (exit_px - entry) if long else (entry - exit_px)
        costs = COST_BPS / 1e4 * entry
        out.update({"entry_fill_assumption": "decision_bar_close (IEX proxy; SIP quote fill later)",
                    "stop_hit": first_hit == "stop", "target_hit": first_hit == "target",
                    "first_hit": first_hit, "minutes_to_first_hit": mins,
                    "gross_pnl_per_unit": round(gross, 4),
                    "modeled_costs_per_unit": round(costs, 4),
                    "net_pnl_per_unit": round(gross - costs, 4),
                    "costs_note": "PLACEHOLDER cost model; real bid/ask+fees modeled in v1"})
    return out


def attach_outcomes(symbol, market_date, session_bars, now=None):
    """Compute outcomes for the day's decisions and write them to a SEPARATE,
    immutable outcomes log keyed by decision_id. Never mutates the decisions log.
    Idempotent; a re-run for an already-scored decision is marked and skipped
    from the primary set."""
    now = now or datetime.now(timezone.utc)
    decisions = _read_jsonl(DECISIONS_LOG, symbol, market_date)
    existing = {o["decision_id"] for o in _read_jsonl(OUTCOMES_LOG, symbol, market_date)}
    written = 0
    for d in decisions:
        if d["decision_id"] in existing:
            continue   # already scored — do not duplicate
        oc = compute_decision_outcome(d, session_bars)
        rec = {"decision_id": d["decision_id"], "symbol": d["symbol"],
               "market_date": str(market_date), "decision": d["decision"],
               "outcomes_recorded_at": now.astimezone(timezone.utc).isoformat(),
               "outcome": oc, "formal_cohort_eligible": FORMAL_COHORT_ELIGIBLE,
               "is_rerun": False}
        try:
            with open(OUTCOMES_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
            written += 1
        except OSError:
            pass
    return {"attached": written, "decisions_seen": len(decisions),
            "already_scored": len(existing)}


def exploratory_summary(symbol="SPY"):
    """Descriptive read over decisions + outcomes. For the FIRST 3–5 sessions this
    is operational validation only — NOT a profitability or edge claim."""
    decisions = _read_jsonl(DECISIONS_LOG, symbol)
    outcomes = {o["decision_id"]: o["outcome"] for o in _read_jsonl(OUTCOMES_LOG, symbol)}
    by_dec = {}
    for d in decisions:
        oc = outcomes.get(d["decision_id"])
        by_dec.setdefault(d["decision"], []).append(oc)

    def med_ret(recs, key="return_60m"):
        xs = sorted(o[key] for o in recs if o and o.get(key) is not None)
        if not xs:
            return None
        m = len(xs) // 2
        return round(xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2, 6)

    sessions = len({d["market_date"] for d in decisions})

    # candidate-outcome breakdown (ambiguous_same_bar kept separate, never a win)
    cand_ocs = [outcomes.get(d["decision_id"]) for d in decisions
                if d["decision"] in ("LONG_CANDIDATE", "SHORT_CANDIDATE")]
    tally = {"target_first": 0, "stop_first": 0, "ambiguous_same_bar": 0,
             "no_hit": 0, "incomplete": 0}
    mfes, maes = [], []
    for oc in cand_ocs:
        if not oc or not oc.get("available"):
            tally["incomplete"] += 1; continue
        fh = oc.get("first_hit")
        if fh == "target": tally["target_first"] += 1
        elif fh == "stop": tally["stop_first"] += 1
        elif fh == "ambiguous_same_bar": tally["ambiguous_same_bar"] += 1
        else: tally["no_hit"] += 1
        if oc.get("mfe") is not None: mfes.append(oc["mfe"])
        if oc.get("mae") is not None: maes.append(oc["mae"])

    def _med(xs):
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return None
        m = len(xs) // 2
        return round(xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2, 6)

    return {
        "policy_version": POLICY_VERSION,
        "formal_cohort_eligible": FORMAL_COHORT_ELIGIBLE,
        "evaluation_status": "operational_only",
        "symbol": symbol.upper(),
        "sessions": sessions,
        "decision_samples": len(decisions),
        "counts": {k: len(v) for k, v in by_dec.items()},
        "candidate_outcomes": tally,
        "median_return_60m_by_decision": {k: med_ret(v) for k, v in by_dec.items()},
        "candidate_median_mfe": _med(mfes),
        "candidate_median_mae": _med(maes),
        "modeled_costs": "placeholder_only",
        "promotable_to_entry_policy_v1": False,
        "stage": ("operational_validation" if sessions < 5 else "descriptive_observation"),
        "note": ("Descriptive only, non-qualifying historical evidence. For the first 3–5 sessions this "
                 "verifies machinery — timestamp alignment, no future-data in entries, stop/"
                 "target ordering, abstention on missing bars, no duplicate outcomes, reproducible "
                 "EOD processing — NOT profitability. Candidate median forward return being higher "
                 "than WAIT/NO_TRADE would be suggestive, never conclusive at this scale."),
    }
