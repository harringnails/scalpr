"""
PREMARKET ASSESSMENT — a structured, honest trade-readiness scorecard.

The old precheck panel answered "how many macro signals lean bullish?" This
answers the practical question a scalper actually has before the open:

    Is the background favorable, is the current LOCATION a good entry, and is
    the market liquid and stable enough to execute — right now?

Those are separate axes, so this produces six independent assessments plus a
data-confidence score:

    Regime         trending / mean-reverting / unstable
    Direction      bullish / bearish / balanced
    Confirmation   do breadth, sectors, credit, volatility agree?
    Entry quality  is price well located vs levels / VWAP / ATR?
    Tradability    liquidity, spreads, event risk acceptable?
    Event risk     scheduled releases during the session
    Data confidence  how much of the above rests on real vs proxied/missing data

Design rule that matters most (per review): a DEGRADED or UNAVAILABLE input
lowers data confidence — it is NEVER silently counted as "neutral". Otherwise
six missing indicators could make the panel look falsely balanced. Missing
inputs are excluded from directional aggregation and disclosed, not averaged in.

This is an uncalibrated structured evidence summary, not a probability. No
component is weighted by predictive power because that power has not been
measured. Weights come only after walk-forward validation.

Data status vocabulary, applied to every input and component:
    available    reliable data, normal calculation
    degraded     usable proxy or partial data (e.g. thin IEX premarket)
    unavailable  no data on the current feed — excluded, reduces confidence
    stale        last update older than the allowed age

The module is built so a future data feed (breadth, VIX term structure, auction
imbalance, an event calendar) becomes a plug-in upgrade: a currently-UNAVAILABLE
component simply starts returning AVAILABLE, with no redesign.
"""

import statistics
import time
from datetime import datetime, timedelta, timezone

from precheck import _closes, _sma, _chg, _vol, _vol_pct

AVAILABLE = "available"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"
STALE = "stale"
_STATUS_RANK = {AVAILABLE: 3, DEGRADED: 2, STALE: 1, UNAVAILABLE: 0}

# how much each data status is worth toward the data-confidence score
_STATUS_WEIGHT = {AVAILABLE: 1.0, DEGRADED: 0.5, STALE: 0.25, UNAVAILABLE: 0.0}

SCORECARD_VERSION = "premarket-scorecard-v1"   # frozen; changes here start a new shadow cohort
CACHE_SECONDS = 60
_cache = {}

# expanded ETF universe (all fetchable as daily bars on the current feed)
LEADERSHIP_ETFS = ["QQQ", "IWM", "RSP", "SMH", "XLF", "XLI", "XLY",
                   "XLP", "XLV", "XLU", "HYG", "LQD", "TLT", "SHY", "VIXY"]

# ── decision policy ────────────────────────────────────────────────────────
# The scorecard has no numeric WEIGHTS, but the categorical rule that turns the
# six axes into a conclusion is still a decision policy. It is versioned and
# published in the output so the interpretation logic can't drift silently.
POLICY_VERSION = "premarket-policy-v1"
MIN_DATA_CONFIDENCE = 0.50            # below this, conclusion is capped at insufficient_confidence
TRADABILITY_REQUIRED_FOR_ENTRY = True  # execution conditions must be confirmed to be entry-ready
EVENT_RISK_CAN_VETO = True             # an elevated scheduled-event window forces WAIT
UNSTABLE_REGIME_FORCES_WAIT = True
POOR_LOCATION_FORCES_WAIT = True


# ── small helpers ─────────────────────────────────────────────────────────

def _ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = vals[-n]
    for v in vals[-n + 1:]:
        e = v * k + e * (1 - k)
    return e


def _slope(vals, n):
    """Sign+magnitude of an n-bar change in the EMA, as % — a crude slope."""
    if len(vals) < n + 1:
        return None
    a, b = _ema(vals[:-1], n), _ema(vals, n)
    if a is None or b is None or a == 0:
        return None
    return (b / a - 1) * 100


def _true_atr(ohlc, n=14):
    """Conventional ATR from OHLC: TR_t = max(H-L, |H-Cprev|, |L-Cprev|),
    averaged over n. This is AVAILABLE data (Alpaca daily bars carry OHLC), not
    a degraded proxy."""
    if len(ohlc) < n + 1:
        return None
    trs = []
    for i in range(len(ohlc) - n, len(ohlc)):
        h, l = ohlc[i]["h"], ohlc[i]["l"]
        c_prev = ohlc[i - 1]["c"]
        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
    return sum(trs) / len(trs) if trs else None


def _worst_status(*statuses):
    """A component is only as trustworthy as its weakest input."""
    present = [s for s in statuses if s]
    if not present:
        return UNAVAILABLE
    return min(present, key=lambda s: _STATUS_RANK.get(s, 0))


def _component(name, status, assessment, detail, direction=None):
    return {"name": name, "status": status, "assessment": assessment,
            "detail": detail, "direction": direction}


# ── data fetch with status tagging ────────────────────────────────────────

def _closes_status(client, symbol, min_full=200):
    c = _closes(client, symbol)
    if not c:
        return [], UNAVAILABLE
    if len(c) < 25:
        return c, UNAVAILABLE
    if len(c) < min_full:
        return c, DEGRADED     # enough for short signals, not for 200-day structure
    return c, AVAILABLE


def _ohlc_status(client, symbol, min_full=200):
    """Daily OHLC bars (Alpaca carries open/high/low/close) -> true ATR and
    prior-day high/low/close levels. AVAILABLE when full history is present."""
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                               start=datetime.now(timezone.utc) - timedelta(days=420))
        bars = client.get_stock_bars(req).data.get(symbol) or []
        ohlc = [{"h": float(b.high), "l": float(b.low), "c": float(b.close)} for b in bars]
    except Exception:
        return [], UNAVAILABLE
    if len(ohlc) < 25:
        return ohlc, UNAVAILABLE
    if len(ohlc) < min_full:
        return ohlc, DEGRADED
    return ohlc, AVAILABLE


def _premarket_minute(client, symbol):
    """Attempt premarket minute bars for VWAP / overnight range / relative
    volume. On the free IEX feed premarket coverage is thin, so a successful
    fetch is DEGRADED at best; an empty/failed fetch is UNAVAILABLE."""
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        start = datetime.now(timezone.utc) - timedelta(hours=16)
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start)
        bars = client.get_stock_bars(req).data.get(symbol) or []
        if len(bars) < 5:
            return None, UNAVAILABLE
        last_ts = getattr(bars[-1], "timestamp", None)
        stale = bool(last_ts and (datetime.now(timezone.utc) - last_ts).total_seconds() > 1800)
        vol = sum(float(getattr(b, "volume", 0) or 0) for b in bars)
        typical = [(float(b.high) + float(b.low) + float(b.close)) / 3 for b in bars]
        vwap = (sum(t * float(getattr(b, "volume", 0) or 0) for t, b in zip(typical, bars)) / vol
                if vol else None)
        highs = [float(b.high) for b in bars]
        lows = [float(b.low) for b in bars]
        data = {"vwap": vwap, "high": max(highs), "low": min(lows),
                "last": float(bars[-1].close), "volume": vol, "bars": len(bars)}
        return data, (STALE if stale else DEGRADED)
    except Exception:
        return None, UNAVAILABLE


# ── the six components ─────────────────────────────────────────────────────

def _regime(sym_closes, status):
    """Trending vs mean-reverting vs unstable, from EMA ordering + realized-vol
    percentile. High vol with no EMA alignment = unstable."""
    if status == UNAVAILABLE or len(sym_closes) < 55:
        return _component("Regime", UNAVAILABLE, "unknown", "insufficient history")
    p = sym_closes[-1]
    e9, e21, e50 = _ema(sym_closes, 9), _ema(sym_closes, 21), _ema(sym_closes, 50)
    vpct = _vol_pct(sym_closes)
    aligned_up = e9 and e21 and e50 and p > e9 > e21 > e50
    aligned_dn = e9 and e21 and e50 and p < e9 < e21 < e50
    unstable = vpct is not None and vpct >= 85 and not (aligned_up or aligned_dn)
    if unstable:
        return _component("Regime", _worst_status(status), "unstable",
                          f"realized vol in the {vpct:.0f}th pct with no clean trend structure")
    if aligned_up or aligned_dn:
        d = "up" if aligned_up else "down"
        return _component("Regime", _worst_status(status), "trending",
                          f"EMAs stacked {d}; vol {'—' if vpct is None else f'{vpct:.0f}th pct'}")
    return _component("Regime", _worst_status(status), "mean_reverting",
                      "EMAs interleaved — no dominant trend")


def _direction(sym, sym_closes, status, gap):
    """Bullish/bearish/balanced from EMA structure + momentum, nudged by the
    overnight gap (degraded). Contributes to directional aggregation."""
    if status == UNAVAILABLE or len(sym_closes) < 21:
        return _component("Direction", UNAVAILABLE, "unknown", "insufficient history", direction="neutral")
    p = sym_closes[-1]
    e9, e21 = _ema(sym_closes, 9), _ema(sym_closes, 21)
    m5, m20 = _chg(sym_closes, 5), _chg(sym_closes, 20)
    bull = sum(1 for c in [p > e9 if e9 else None, e9 > e21 if (e9 and e21) else None,
                           (m5 or 0) > 0, (m20 or 0) > 0] if c)
    bear = sum(1 for c in [p < e9 if e9 else None, e9 < e21 if (e9 and e21) else None,
                           (m5 or 0) < 0, (m20 or 0) < 0] if c)
    st = _worst_status(status, gap.get("status") if gap else None)
    if bull >= 3 and bull > bear:
        return _component("Direction", st, "bullish",
                          f"{bull}/4 short-trend checks up (5d {m5:+.1f}%, 20d {m20:+.1f}%)",
                          direction="bullish")
    if bear >= 3 and bear > bull:
        return _component("Direction", st, "bearish",
                          f"{bear}/4 short-trend checks down (5d {m5:+.1f}%, 20d {m20:+.1f}%)",
                          direction="bearish")
    return _component("Direction", st, "balanced",
                      f"mixed short-trend ({bull} up / {bear} down)", direction="neutral")


def _confirmation(series):
    """Do participation, leadership, credit and volatility AGREE with a risk-on
    tape? Breadth here is the RSP/SPY proxy only (degraded) — true A/D is
    unavailable. Returns a confirming/mixed/diverging read."""
    spy = series.get("SPY", ([], UNAVAILABLE))
    spy_c, spy_s = spy
    votes, details, statuses = [], [], []

    def rel(a, b, label, thresh=0.5, degraded=False):
        ca, sa = series.get(a, ([], UNAVAILABLE))
        cb, sb = series.get(b, ([], UNAVAILABLE))
        st = _worst_status(sa, sb)
        if st == UNAVAILABLE or len(ca) < 11 or len(cb) < 11:
            return None
        diff = (_chg(ca, 10) or 0) - (_chg(cb, 10) or 0)
        statuses.append(DEGRADED if degraded else st)
        v = 1 if diff > thresh else -1 if diff < -thresh else 0
        details.append(f"{label} {diff:+.2f}%/10d")
        return v

    votes.append(rel("RSP", "SPY", "breadth(RSP/SPY proxy)", degraded=True))  # proxy → degraded
    votes.append(rel("IWM", "SPY", "small-caps IWM/SPY"))
    votes.append(rel("QQQ", "SPY", "tech QQQ/SPY"))
    votes.append(rel("SMH", "QQQ", "semis SMH/QQQ"))
    votes.append(rel("XLY", "XLP", "cyclicals XLY/XLP"))   # risk-on vs defensive
    votes.append(rel("HYG", "LQD", "credit HYG/LQD"))
    votes = [v for v in votes if v is not None]
    if not votes:
        return _component("Confirmation", UNAVAILABLE, "unknown", "no leadership pairs available")
    up, dn = votes.count(1), votes.count(-1)
    st = _worst_status(*statuses) if statuses else UNAVAILABLE
    joined = ", ".join(details)
    if up >= 2 and up > dn * 2:
        return _component("Confirmation", st, "confirming", f"risk-on participation: {joined}")
    if dn >= 2 and dn > up * 2:
        return _component("Confirmation", st, "diverging", f"defensive/narrow tape: {joined}")
    return _component("Confirmation", st, "mixed", f"split internals: {joined}")


def _entry_quality(ohlc, status, premkt, pm_status):
    """Location quality from TRUE ATR + prior-day high/low/close (AVAILABLE
    data). Distance from prior-day close, and proximity to prior-day high/low,
    in ATR units. Extended near resistance after a run is 'poor' even in a
    bullish tape — the whole point of this axis. Premarket VWAP is added when
    available and only that sub-part is degraded."""
    if status == UNAVAILABLE or len(ohlc) < 20:
        return _component("Entry quality", UNAVAILABLE, "unknown", "insufficient history")
    atr = _true_atr(ohlc)
    if not atr:
        return _component("Entry quality", DEGRADED, "unknown", "not enough bars for ATR")
    prior = ohlc[-1]                       # last completed daily bar
    prior_close, pd_high, pd_low = prior["c"], prior["h"], prior["l"]
    last = premkt["last"] if premkt and premkt.get("last") else prior_close
    dist_atr = (last - prior_close) / atr
    to_high = (pd_high - last) / atr       # ATR of room up to prior-day high
    to_low = (last - pd_low) / atr
    bits = [f"{dist_atr:+.2f} ATR vs prior close",
            f"{to_high:.2f} ATR to PDH", f"{to_low:.2f} ATR to PDL"]
    st = status                            # true ATR + levels are AVAILABLE
    if premkt and premkt.get("vwap"):
        bits.append(f"{(last - premkt['vwap']) / atr:+.2f} ATR vs premkt VWAP")
        st = _worst_status(st, pm_status)
    if abs(dist_atr) >= 1.0 or 0 <= to_high <= 0.2:
        return _component("Entry quality", st, "poor",
                          f"extended / into resistance: {', '.join(bits)}")
    if abs(dist_atr) <= 0.35:
        return _component("Entry quality", st, "good", f"near reference: {', '.join(bits)}")
    return _component("Entry quality", st, "fair", ", ".join(bits))


def _tradability(premkt, pm_status, event_risk):
    """Liquidity/execution readiness. Relative volume is degraded (thin IEX
    premarket); event risk is UNAVAILABLE (no calendar feed). A restricted
    event window overrides an otherwise-tradable read."""
    if event_risk["status"] == AVAILABLE and event_risk["assessment"] == "elevated":
        return _component("Tradability", AVAILABLE, "restricted",
                          f"held back by event risk: {event_risk['detail']}")
    if not premkt or pm_status == UNAVAILABLE:
        return _component("Tradability", UNAVAILABLE, "unknown",
                          "no premarket volume/quote data on this feed")
    bars = premkt.get("bars", 0)
    return _component("Tradability", pm_status, "provisional",
                      f"premarket data present ({bars} min bars) but depth/auction imbalance "
                      f"unavailable on this feed — treat liquidity as unconfirmed")


def _event_risk():
    """No economic-event calendar is connected, so scheduled-release risk cannot
    be assessed. Reported UNAVAILABLE (which lowers data confidence) rather than
    pretended to be 'low'. Becomes AVAILABLE when a calendar feed is added."""
    return _component("Event risk", UNAVAILABLE, "unknown",
                      "no economic-event calendar connected (CPI/FOMC/payrolls/earnings "
                      "cannot be checked) — do not assume a quiet session")


# ── explicit unavailable capabilities, disclosed not hidden ────────────────

_UNAVAILABLE_CAPABILITIES = [
    ("Advance/decline & up-down volume", "needs full-constituent or breadth feed"),
    ("% of S&P above 20/50/200-day MAs", "needs constituent data"),
    ("VIX9D / VIX / VIX3M term structure", "needs Cboe index feed (VIXY is only a rough proxy)"),
    ("Opening-auction imbalance", "needs exchange auction feed"),
    ("Options OI / skew depth", "needs fuller options data"),
    ("ES futures confirmation", "not on the current plan"),
    ("Economic-event calendar", "no calendar source connected"),
]


# ── assembly ───────────────────────────────────────────────────────────────

def _data_confidence(components):
    """Fraction of full data backing the assessment, from each component's
    status. Unavailable components pull this DOWN — they are not treated as
    neutral. This is the guardrail against a falsely-balanced panel."""
    if not components:
        return 0.0, "none"
    score = sum(_STATUS_WEIGHT.get(c["status"], 0.0) for c in components) / len(components)
    band = ("high" if score >= 0.75 else "moderate" if score >= 0.5
            else "low" if score >= 0.25 else "very low")
    return round(score, 3), band


def _background_label(direction, confirmation):
    """The market-direction summary, independent of whether the trade is
    executable — so a 'WAIT' due to tradability isn't mistaken for bearish."""
    d, c = direction["assessment"], confirmation["assessment"]
    if d == "bullish":
        return "favorable" if c == "confirming" else "leaning_bullish"
    if d == "bearish":
        return "unfavorable" if c == "diverging" else "leaning_bearish"
    return "mixed"


def _readiness(direction, entry, confirmation, tradability, regime, conf_score):
    """Turn the axes into a conclusion under the published policy. A good
    background never becomes 'go' on its own; and the BLOCKING condition names
    tradability/location only when there is actually a directional setup being
    blocked (so a merely mixed tape reads 'mixed', not 'blocked')."""
    background = _background_label(direction, confirmation)
    if conf_score < MIN_DATA_CONFIDENCE:
        return ("insufficient_confidence",
                "Too much of the picture is proxied or missing to act on — treat as context only.",
                "data_confidence_below_minimum", background)
    if UNSTABLE_REGIME_FORCES_WAIT and regime["assessment"] == "unstable":
        return ("wait", "Unstable volatility regime — stand aside until it settles.",
                "unstable_regime", background)

    has_setup = (direction["direction"] in ("bullish", "bearish")
                 and confirmation["assessment"] == "confirming")
    if not has_setup:
        return ("mixed", "No clean alignment across background, confirmation and location.",
                None, background)

    # There IS a directional, confirmed setup — now the execution/location gates,
    # each naming why it's not yet actionable.
    if tradability["assessment"] == "restricted":
        return ("wait", "Setup present, but execution is restricted by event risk.",
                "event_risk_restricted", background)
    if TRADABILITY_REQUIRED_FOR_ENTRY and tradability["assessment"] != "adequate":
        return ("wait", "Directional setup with confirmation, but execution conditions "
                        "(liquidity/depth/auction) can't be confirmed on this feed — verify "
                        "the option spread and size manually before entering.",
                "tradability_unconfirmed", background)
    if POOR_LOCATION_FORCES_WAIT and entry["assessment"] == "poor":
        return ("wait_for_location",
                "Setup present, but current location is poor — wait for a better level.",
                "poor_location", background)
    return ("favorable_with_confirmation",
            f"{direction['assessment'].title()} background with internal confirmation, "
            f"acceptable location and confirmed execution.", None, background)


def run_premarket(data_client, symbol, direction_hint=None):
    key = symbol.upper()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    # symbol: fetch OHLC (for true ATR + prior-day levels); closes derive from it
    sym_ohlc, sym_status = _ohlc_status(data_client, key)
    sym_closes = [b["c"] for b in sym_ohlc]
    series = {"SPY": _closes_status(data_client, "SPY")}
    for etf in LEADERSHIP_ETFS:
        series[etf] = _closes_status(data_client, etf)

    premkt, pm_status = _premarket_minute(data_client, key)
    gap = None
    if premkt and premkt.get("last") and sym_closes:
        gap_pct = (premkt["last"] / sym_closes[-1] - 1) * 100
        gap = {"pct": round(gap_pct, 3), "status": pm_status}

    event_risk = _event_risk()
    regime = _regime(sym_closes, sym_status)
    direction = _direction(key, sym_closes, sym_status, gap)
    confirmation = _confirmation(series)
    entry = _entry_quality(sym_ohlc, sym_status, premkt, pm_status)
    tradability = _tradability(premkt, pm_status, event_risk)

    components = [regime, direction, confirmation, entry, tradability, event_risk]
    conf_score, conf_band = _data_confidence(components)
    readiness, readiness_text, blocking, background = _readiness(
        direction, entry, confirmation, tradability, regime, conf_score)

    result = {
        "symbol": key,
        "scorecard_version": SCORECARD_VERSION,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "assessments": {
            "regime": regime, "direction": direction, "confirmation": confirmation,
            "entry_quality": entry, "tradability": tradability, "event_risk": event_risk,
        },
        "gap": gap,
        "data_confidence": {"score": conf_score, "band": conf_band},
        "background": background,
        "readiness": readiness, "readiness_text": readiness_text,
        "blocking_condition": blocking,
        "decision_policy": {
            "version": POLICY_VERSION, "type": "rule_based_uncalibrated",
            "rules": {
                "minimum_data_confidence": MIN_DATA_CONFIDENCE,
                "tradability_required_for_entry_ready": TRADABILITY_REQUIRED_FOR_ENTRY,
                "event_risk_can_veto": EVENT_RISK_CAN_VETO,
                "unstable_regime_forces_wait": UNSTABLE_REGIME_FORCES_WAIT,
                "poor_location_forces_wait": POOR_LOCATION_FORCES_WAIT,
            },
        },
        "unavailable_capabilities": [{"name": n, "reason": r}
                                     for n, r in _UNAVAILABLE_CAPABILITIES],
        "disclaimer": ("Uncalibrated structured evidence summary — not a probability, forecast, or "
                       "recommendation. Components are NOT weighted by predictive power (unmeasured). "
                       "Degraded/unavailable inputs lower data confidence; they are never counted as "
                       "neutral. A favorable background is not by itself a good entry."),
    }
    _cache[key] = (time.time(), result)
    return result
