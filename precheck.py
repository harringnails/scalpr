"""PRE-TRADE CHECK - descriptive evidence panel for Scalpr Trade.

The headline is a versioned, horizon-aware weighted-evidence agreement, not a
forecast or calibrated probability. Its policy weights describe evidence
proximity and prevent every proxy from receiving an equal vote; they are not
claimed predictive power. Missing inputs stay explicit, weak coverage or
mixed tactical evidence produces NO READ, and this module never blocks, sizes,
submits, or exits a trade.
"""

import hashlib
import json
import re
import statistics
import time
from datetime import datetime, timedelta, timezone

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

MIN_SIGNALS = 4
CACHE_SECONDS = 60
YES_NO_THRESHOLD = 0.60
MIN_TACTICAL_COVERAGE = 0.70
WEIGHTED_SCORING_VERSION = "precheck-weighted-evidence-v0"
HORIZONS = ("tactical", "structural", "macro")
_cache = {}

# Canonical signal names/keys, shared with scalp_server.py's journal columns
# and journal_model.py's scorer, so all three stay in lockstep if a signal
# is ever added or renamed here.
SIGNAL_NAMES = ["Trend", "Momentum", "Volatility", "Fear gauge", "Breadth",
                "Tech leadership", "Rates", "Credit"]
SIGNAL_KEYS = [n.lower().replace(" ", "_") for n in SIGNAL_NAMES]
SNAPSHOT_KEYS = ["verdict", "tone", "supporting", "opposing", "caution"] + SIGNAL_KEYS

# Fixed policy weights are intentionally small integers and describe how close
# an input is to the tactical price question. They are not fitted coefficients.
SIGNAL_POLICY = {
    "Trend": {"weight": 3, "horizon": "tactical", "group": None,
              "bias_note": None, "critical": True},
    "Momentum": {"weight": 3, "horizon": "tactical", "group": None,
                 "bias_note": None, "critical": True},
    "Volatility": {"weight": 1, "horizon": "tactical", "group": None,
                   "bias_note": None, "critical": False},
    "Fear gauge": {"weight": 1, "horizon": "tactical", "group": None,
                   "bias_note": ("VIXY can decay from contango independently of spot fear; "
                                 "treat it as a low-weight proxy."), "critical": False},
    "Breadth": {"weight": 2, "horizon": "structural", "group": "concentration",
                "bias_note": None, "critical": False},
    "Tech leadership": {"weight": 2, "horizon": "structural",
                        "group": "concentration", "bias_note": None,
                        "critical": False},
    "Rates": {"weight": 1, "horizon": "macro", "group": None,
              "bias_note": ("TLT price is an imperfect proxy for long-rate pressure."),
              "critical": False},
    "Credit": {"weight": 1, "horizon": "macro", "group": None,
               "bias_note": ("HYG price is not a credit-spread series; use as a "
                             "low-weight risk proxy."), "critical": False},
}
WEIGHTED_SCORING_CONFIG_HASH = hashlib.sha256(json.dumps(
    {"version": WEIGHTED_SCORING_VERSION, "threshold": YES_NO_THRESHOLD,
     "min_tactical_coverage": MIN_TACTICAL_COVERAGE, "signals": SIGNAL_POLICY},
    sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def flatten_signals(snap):
    """Turn a run_precheck() result into a flat {key: value} dict keyed by
    SNAPSHOT_KEYS — plain columns instead of nested JSON, for the CSV journal
    and for the scorer. snap may be None (fetch failed, or a trade logged
    before this existed) — every field comes back blank rather than raising."""
    if not snap:
        return {k: "" for k in SNAPSHOT_KEYS}
    by_name = {s["name"]: s["direction"] for group in
               (snap.get("supporting", []), snap.get("opposing", []),
                snap.get("caution", []), snap.get("neutral", [])) for s in group}
    counts = snap.get("counts", {})
    out = {
        "verdict": snap.get("verdict", ""), "tone": snap.get("tone", ""),
        "supporting": counts.get("supporting", ""), "opposing": counts.get("opposing", ""),
        "caution": counts.get("caution", ""),
    }
    for name, key in zip(SIGNAL_NAMES, SIGNAL_KEYS):
        out[key] = by_name.get(name, "missing")
    return out


def _closes(client, symbol, days=420):
    try:
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                               start=datetime.now(timezone.utc) - timedelta(days=days))
        return [float(b.close) for b in (client.get_stock_bars(req).data.get(symbol) or [])]
    except Exception:
        return []


def _sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def _chg(v, n):
    return None if len(v) <= n or v[-(n + 1)] == 0 else (v[-1] / v[-(n + 1)] - 1) * 100


def _vol(v, n=20):
    if len(v) < n + 1:
        return None
    r = [v[i] / v[i - 1] - 1 for i in range(len(v) - n, len(v))]
    return statistics.pstdev(r) * (252 ** 0.5) * 100 if len(r) > 1 else None


def _vol_pct(v, window=20, history=252):
    if len(v) < window + history:
        return None
    reads = [x for x in (_vol(v[:e], window) for e in range(len(v) - history, len(v) + 1)) if x]
    if len(reads) < 30:
        return None
    return sum(1 for r in reads if r <= reads[-1]) / len(reads) * 100


def underlying_of(symbol):
    m = re.match(r"^([A-Z]+)\d{6}[CP]\d{8}$", symbol.upper())
    return m.group(1) if m else symbol.upper()


def implied_direction(symbol, trade_type):
    if trade_type == "option":
        m = re.match(r"^[A-Z]+\d{6}([CP])\d{8}$", symbol.upper())
        if m:
            return "bullish" if m.group(1) == "C" else "bearish"
    return "bullish"


def _sig(name, direction, text):
    policy = SIGNAL_POLICY[name]
    return {
        "name": name, "direction": direction, "text": text,
        "weight": policy["weight"], "horizon": policy["horizon"],
        "group": policy["group"], "bias_note": policy["bias_note"],
    }


def _horizon_score(signals, direction, horizon):
    """Return a transparent relative-to-position score for one horizon."""
    other = "bearish" if direction == "bullish" else "bullish"
    policy_names = [name for name in SIGNAL_NAMES
                    if SIGNAL_POLICY[name]["horizon"] == horizon]
    by_name = {signal["name"]: signal for signal in signals
               if signal.get("name") in policy_names}
    possible_weight = sum(SIGNAL_POLICY[name]["weight"] for name in policy_names)
    available_weight = sum(SIGNAL_POLICY[name]["weight"] for name in by_name)
    support_weight = 0.0
    opposing_weight = 0.0
    coherence = []

    def relative(signal):
        if signal.get("direction") == direction:
            return 1
        if signal.get("direction") == other:
            return -1
        return 0

    grouped = {}
    for name, signal in by_name.items():
        policy = SIGNAL_POLICY[name]
        if policy["group"]:
            grouped.setdefault(policy["group"], []).append(signal)
            continue
        rel = relative(signal)
        if rel > 0:
            support_weight += policy["weight"]
        elif rel < 0:
            opposing_weight += policy["weight"]

    for group, members in sorted(grouped.items()):
        configured = [name for name in policy_names
                      if SIGNAL_POLICY[name]["group"] == group]
        directional = [(signal, relative(signal)) for signal in members
                       if relative(signal)]
        if not directional:
            coherence.append({
                "group": group, "status": "non_directional",
                "detail": f"{group}: no directional members",
            })
            continue
        if len(directional) == 1:
            signal, rel = directional[0]
            weight = SIGNAL_POLICY[signal["name"]]["weight"]
            if rel > 0:
                support_weight += weight
            else:
                opposing_weight += weight
            coherence.append({
                "group": group, "status": "partial",
                "detail": (f"{group}: 1/{len(configured)} directional; "
                           "counted once"),
            })
            continue
        directions = {rel for _, rel in directional}
        if len(directions) == 1:
            rel = directional[0][1]
            weight = max(SIGNAL_POLICY[signal["name"]]["weight"]
                         for signal, _ in directional)
            if rel > 0:
                support_weight += weight
                side = "support"
            else:
                opposing_weight += weight
                side = "oppose"
            coherence.append({
                "group": group, "status": "coherent",
                "detail": (f"{group}: {len(directional)}/{len(configured)} {side}; "
                           f"collapsed to weight {weight}"),
            })
        else:
            # Mixed correlated evidence retains direction but receives half
            # weight so disagreement cannot masquerade as confirmation.
            for signal, rel in directional:
                weight = SIGNAL_POLICY[signal["name"]]["weight"] * 0.5
                if rel > 0:
                    support_weight += weight
                else:
                    opposing_weight += weight
            coherence.append({
                "group": group, "status": "mixed",
                "detail": f"{group}: mixed; directional weights halved",
            })

    directional_weight = support_weight + opposing_weight
    support_share = support_weight / directional_weight if directional_weight else None
    opposing_share = opposing_weight / directional_weight if directional_weight else None
    if support_share is not None and support_share >= YES_NO_THRESHOLD:
        decision, agreement = "YES", support_share
    elif opposing_share is not None and opposing_share >= YES_NO_THRESHOLD:
        decision, agreement = "NO", opposing_share
    else:
        decision = "NO READ"
        agreement = max(support_share or 0.0, opposing_share or 0.0) if directional_weight else None
    return {
        "horizon": horizon,
        "decision": decision,
        "agreement_pct": round(agreement * 100, 1) if agreement is not None else None,
        "support_weight": support_weight,
        "opposing_weight": opposing_weight,
        "directional_weight": directional_weight,
        "available_weight": available_weight,
        "possible_weight": possible_weight,
        "coverage_pct": round(available_weight / possible_weight * 100, 1)
                        if possible_weight else 0.0,
        "coherence": coherence,
    }


def build_weighted_read(signals, missing, direction):
    """Build the non-probabilistic YES/NO/NO READ dashboard contract."""
    if direction not in {"bullish", "bearish"}:
        raise ValueError("direction must be bullish or bearish")
    horizons = {name: _horizon_score(signals, direction, name)
                for name in HORIZONS}
    tactical = horizons["tactical"]
    present = {signal["name"] for signal in signals}
    missing_critical = [name for name, policy in SIGNAL_POLICY.items()
                        if policy["critical"] and name not in present]
    reasons = []
    decision = tactical["decision"]
    if len(signals) < MIN_SIGNALS:
        reasons.append(f"only {len(signals)} of {len(SIGNAL_NAMES)} inputs available")
    if missing_critical:
        reasons.append("critical tactical inputs unavailable: " + ", ".join(missing_critical))
    if tactical["coverage_pct"] < MIN_TACTICAL_COVERAGE * 100:
        reasons.append(f"tactical input coverage is {tactical['coverage_pct']:.0f}%")
    if tactical["directional_weight"] == 0:
        reasons.append("tactical inputs have no directional read")
    elif tactical["decision"] == "NO READ":
        reasons.append(f"neither tactical side reaches {YES_NO_THRESHOLD * 100:.0f}%")
    if reasons:
        decision = "NO READ"

    bias_notes = [{"signal": signal["name"], "note": signal["bias_note"]}
                  for signal in signals if signal.get("bias_note")]
    modifiers = []
    for name in ("structural", "macro"):
        score = horizons[name]
        if score["decision"] == "YES":
            text = f"{name} evidence supports the {direction} position"
        elif score["decision"] == "NO":
            text = f"{name} evidence opposes the {direction} position"
        else:
            text = f"{name} evidence has no clear directional read"
        modifiers.append({"horizon": name, "decision": score["decision"], "text": text})

    return {
        "version": WEIGHTED_SCORING_VERSION,
        "config_hash": WEIGHTED_SCORING_CONFIG_HASH,
        "decision": decision,
        "agreement_pct": tactical["agreement_pct"],
        "input_coverage_pct": tactical["coverage_pct"],
        "basis": "tactical",
        "threshold_pct": YES_NO_THRESHOLD * 100,
        "is_probability": False,
        "is_recommendation": False,
        "reasons": reasons,
        "missing": list(missing),
        "missing_critical": missing_critical,
        "horizons": horizons,
        "coherence": [item for score in horizons.values()
                      for item in score["coherence"]],
        "bias_notes": bias_notes,
        "modifiers": modifiers,
        "label": "weighted evidence agreement - not win probability",
    }


def sig_trend(sym, c):
    p, s20, s50, s200 = c[-1], _sma(c, 20), _sma(c, 50), _sma(c, 200)
    if not s20 or not s50:
        return None
    above = [n for n, s in (("20", s20), ("50", s50), ("200", s200)) if s and p > s]
    total = 3 if s200 else 2
    if len(above) == total:
        return _sig("Trend", "bullish", f"{sym} is above all {total} of its 20/50/200-day averages")
    if not above:
        return _sig("Trend", "bearish", f"{sym} is below all {total} of its 20/50/200-day averages")
    return _sig("Trend", "neutral",
                f"{sym} is above {len(above)} of {total} moving averages ({', '.join(above)}-day) - mixed")


def sig_momentum(sym, c):
    a, b = _chg(c, 5), _chg(c, 20)
    if a is None or b is None:
        return None
    d = "bullish" if a > 0 and b > 0 else "bearish" if a < 0 and b < 0 else "neutral"
    return _sig("Momentum", d, f"{sym} is {a:+.2f}% over 5 days and {b:+.2f}% over 20 days")


def sig_volatility(sym, c):
    v, pct = _vol(c, 20), _vol_pct(c)
    if v is None:
        return None
    if pct is None:
        return _sig("Volatility", "neutral", f"{sym} 20-day realized volatility is {v:.1f}% annualized")
    if pct >= 80:
        return _sig("Volatility", "caution",
                    f"{sym} realized volatility is {v:.1f}% - higher than {pct:.0f}% of the past year. "
                    f"Wider swings both ways.")
    if pct <= 20:
        return _sig("Volatility", "caution",
                    f"{sym} realized volatility is {v:.1f}% - quieter than {100 - pct:.0f}% of the past "
                    f"year. Compression can precede expansion.")
    return _sig("Volatility", "neutral", f"{sym} realized volatility is {v:.1f}%, near its one-year midpoint")


def sig_fear(v):
    ch = _chg(v, 5)
    if ch is None:
        return None
    if ch > 5:
        return _sig("Fear gauge", "bearish", f"VIXY is up {ch:+.1f}% over 5 days - hedging demand rising")
    if ch < -5:
        return _sig("Fear gauge", "bullish", f"VIXY is down {ch:+.1f}% over 5 days - hedging demand easing")
    return _sig("Fear gauge", "neutral", f"VIXY is {ch:+.1f}% over 5 days - little change in fear")


def sig_breadth(spy, rsp):
    a, b = _chg(spy, 10), _chg(rsp, 10)
    if a is None or b is None:
        return None
    s = a - b
    if s > 1.0:
        return _sig("Breadth", "caution",
                    f"Cap-weighted SPY is outpacing equal-weight RSP by {s:+.2f}% over 10 days - "
                    f"gains concentrated in fewer names")
    if s < -1.0:
        return _sig("Breadth", "bullish",
                    f"Equal-weight RSP is outpacing SPY by {abs(s):.2f}% over 10 days - broad participation")
    return _sig("Breadth", "neutral", f"SPY and RSP within {abs(s):.2f}% over 10 days - even participation")


def sig_leadership(qqq, smh):
    a, b = _chg(qqq, 10), _chg(smh, 10)
    if a is None or b is None:
        return None
    s = b - a
    if s > 1.5:
        return _sig("Tech leadership", "bullish",
                    f"Semis (SMH) leading QQQ by {s:+.2f}% over 10 days - risk appetite in tech")
    if s < -1.5:
        return _sig("Tech leadership", "bearish",
                    f"Semis (SMH) lagging QQQ by {abs(s):.2f}% over 10 days - leadership narrowing")
    return _sig("Tech leadership", "neutral", f"Semis tracking QQQ within {abs(s):.2f}% over 10 days")


def sig_rates(tlt):
    ch = _chg(tlt, 10)
    if ch is None:
        return None
    if ch < -1.5:
        return _sig("Rates", "bearish",
                    f"TLT is {ch:+.2f}% over 10 days - long rates rising, a headwind for equity multiples")
    if ch > 1.5:
        return _sig("Rates", "bullish",
                    f"TLT is {ch:+.2f}% over 10 days - long rates falling, easing pressure on equities")
    return _sig("Rates", "neutral", f"TLT is {ch:+.2f}% over 10 days - long rates roughly steady")


def sig_credit(hyg):
    ch = _chg(hyg, 10)
    if ch is None:
        return None
    if ch < -1.0:
        return _sig("Credit", "bearish",
                    f"High-yield credit (HYG) is {ch:+.2f}% over 10 days - credit tightening")
    if ch > 1.0:
        return _sig("Credit", "bullish",
                    f"High-yield credit (HYG) is {ch:+.2f}% over 10 days - risk appetite firm")
    return _sig("Credit", "neutral", f"High-yield credit (HYG) is {ch:+.2f}% over 10 days - credit stable")


def run_precheck(data_client, symbol, trade_type="stock", direction=None):
    sym = underlying_of(symbol)
    direction = direction or implied_direction(symbol, trade_type)
    key = (sym, direction)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    need = {sym, "SPY", "QQQ", "RSP", "SMH", "TLT", "HYG", "VIXY"}
    c = {s: _closes(data_client, s) for s in need}

    plan = [
        ("Trend", sig_trend, [sym], True),
        ("Momentum", sig_momentum, [sym], True),
        ("Volatility", sig_volatility, [sym], True),
        ("Fear gauge", sig_fear, ["VIXY"], False),
        ("Breadth", sig_breadth, ["SPY", "RSP"], False),
        ("Tech leadership", sig_leadership, ["QQQ", "SMH"], False),
        ("Rates", sig_rates, ["TLT"], False),
        ("Credit", sig_credit, ["HYG"], False),
    ]

    signals, missing = [], []
    for label, fn, names, needs_sym in plan:
        series = [c.get(n) or [] for n in names]
        if any(len(x) < 25 for x in series):
            missing.append(label)
            continue
        try:
            out = fn(*([sym] + series if needs_sym else series))
        except Exception:
            out = None
        (signals if out else missing).append(out or label)

    other = "bearish" if direction == "bullish" else "bullish"
    sup = [s for s in signals if s["direction"] == direction]
    opp = [s for s in signals if s["direction"] == other]
    cau = [s for s in signals if s["direction"] == "caution"]
    neu = [s for s in signals if s["direction"] == "neutral"]

    if len(signals) < MIN_SIGNALS:
        verdict, tone = "Not enough data for a read", "abstain"
    elif not sup and not opp:
        verdict, tone = "No directional signals - conditions are neutral", "mixed"
    elif len(sup) >= 3 and len(sup) >= 2 * len(opp):
        verdict, tone = f"Most checks align with a {direction} position", "aligned"
    elif len(opp) >= 3 and len(opp) >= 2 * len(sup):
        verdict, tone = f"Most checks lean against a {direction} position", "against"
    else:
        verdict, tone = "Mixed - the checks disagree, no clear read", "mixed"

    weighted_read = build_weighted_read(signals, missing, direction)
    result = {
        "symbol": sym, "direction": direction, "verdict": verdict, "tone": tone,
        "legacy_summary": verdict,
        "counts": {"supporting": len(sup), "opposing": len(opp),
                   "caution": len(cau), "neutral": len(neu)},
        "supporting": sup, "opposing": opp, "caution": cau, "neutral": neu,
        "unavailable": missing,
        "weighted_read": weighted_read,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "disclaimer": ("Weighted evidence agreement is a descriptive policy score - not a win "
                       "probability, forecast, recommendation, or measured predictive edge. "
                       "Daily bars lag the live market."),
    }
    _cache[key] = (time.time(), result)
    return result
