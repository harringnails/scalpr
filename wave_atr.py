"""
intraday-atr-v0 — versioned intraday volatility measure for Wave Riding.

A NEW measure, deliberately NOT `premarket._true_atr` (which is a DAILY ATR).
Wave distance is measured against this ATR, computed on the UNDERLYING from
COMPLETED 5-minute regular-session bars. The value is frozen per wave by the
engine (goalpost never moves mid-wave).

Rules:
  * 5-minute bars, 14 completed bars, ATR = mean True Range over the period.
  * The currently forming bar is EXCLUDED (only completed bars count).
  * Regular-session bars only by default (premarket only if config enables).
  * Warm-up: <5 bars → WARMUP_BLOCKED (no add); 5–13 → PARTIAL_WARMUP; ≥14 → FULL.
  * Computed on the underlying, not the option (option premium volatility is
    contaminated by delta/gamma/IV/theta/spread).
"""

INTRADAY_ATR_VERSION = "intraday-atr-v0"


def resample_to_5m(minute_bars, now_minute=None):
    """Aggregate 1-minute session bars into COMPLETED 5-minute bars.

    `minute_bars`: [{"t": minutes_from_open, "high","low","close"}] (regular
    session). Returns [{"bucket","high","low","close"}] EXCLUDING the currently
    forming 5-min bucket. `now_minute` (latest observed minute-from-open) marks
    the forming bucket = now_minute // 5; if None, the last seen bucket is treated
    as still forming and dropped (conservative)."""
    if not minute_bars:
        return []
    buckets = {}
    for b in minute_bars:
        k = int(b["t"]) // 5
        d = buckets.get(k)
        if d is None:
            buckets[k] = {"bucket": k, "high": b["high"], "low": b["low"],
                          "close": b["close"], "tmax": b["t"]}
        else:
            d["high"] = max(d["high"], b["high"])
            d["low"] = min(d["low"], b["low"])
            if b["t"] >= d["tmax"]:
                d["tmax"], d["close"] = b["t"], b["close"]
    forming = (int(now_minute) // 5) if now_minute is not None else max(buckets)
    return [{"bucket": buckets[k]["bucket"], "high": buckets[k]["high"],
             "low": buckets[k]["low"], "close": buckets[k]["close"]}
            for k in sorted(buckets) if k < forming]


def _true_ranges(bars5):
    trs, prev_close = [], None
    for b in bars5:
        if prev_close is None:
            tr = b["high"] - b["low"]
        else:
            tr = max(b["high"] - b["low"], abs(b["high"] - prev_close),
                     abs(b["low"] - prev_close))
        trs.append(tr)
        prev_close = b["close"]
    return trs


def compute_intraday_atr(bars5, period=14, warmup_min=5, full=14, as_of=None):
    """ATR + quality/audit from COMPLETED 5-min bars. Returns the audit dict used
    by the engine to freeze the wave goalpost."""
    n = len(bars5)
    base = {"atr_version": INTRADAY_ATR_VERSION, "atr_timeframe": "5min",
            "atr_bar_count": n, "atr_as_of": as_of}
    if n < warmup_min:
        return {**base, "atr_value": None, "atr_period_used": 0,
                "atr_quality": "WARMUP_BLOCKED"}
    trs = _true_ranges(bars5)
    use = trs[-period:] if n >= period else trs
    atr = sum(use) / len(use)
    return {**base, "atr_value": round(atr, 6), "atr_period_used": len(use),
            "atr_quality": "FULL" if n >= full else "PARTIAL_WARMUP"}


def atr_from_minute_bars(minute_bars, now_minute=None, period=14, warmup_min=5,
                         full=14, as_of=None):
    """Convenience: resample 1-min → completed 5-min, then compute ATR."""
    bars5 = resample_to_5m(minute_bars, now_minute=now_minute)
    return compute_intraday_atr(bars5, period=period, warmup_min=warmup_min,
                                full=full, as_of=as_of)
