"""
MICRO READ — a short-horizon slope/imbalance calculation off the tick logger.

What this is: an ordinary-least-squares line fit to the last WINDOW_SECONDS
of SPY's mid-price, plus two supporting stats: quote-size imbalance (from
resting quote size, not traded order flow) from
resting bid/ask size, and a z-score of the current price against its own
short-term mean. The slope answers "which way has it been moving and how
fast" with actual math (equations below). It does not answer "which way
will it move next" — that would require validating the slope against what
actually happened afterward, over many windows, which the tick log hasn't
accumulated enough history to do yet.

The honesty mechanism, same as precheck.py and journal_model.py: the R²
of the fit is checked before a direction is asserted at all. A slope
computed from a nearly-flat scatter (low R²) is not a trend, it's a line
drawn through noise — so below R2_FLOOR this abstains to "choppy" rather
than reporting a confident-looking number that isn't one.

Equations used, in plain terms:
  slope   = cov(t, price) / var(t)            — OLS drift, price units/sec
  r2      = 1 - SS_residual / SS_total         — how much of the scatter the line explains
  imbalance = mean[(bid_size - ask_size) / (bid_size + ask_size)]  — range -1..+1
  z       = (last_price - mean(price)) / stdev(price)  — how stretched vs. the window's own mean
"""

import csv
import math
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

TICK_LOG = Path("tick_log.csv")
WINDOW_SECONDS = 120     # matches the "last 2 minutes" framing this was asked about
MIN_TICKS = 20           # need at least this many points before fitting anything
R2_FLOOR = 0.15          # below this, the "trend" is indistinguishable from noise
BUFFER_MAXLEN = 900       # 15 minutes at the current one-snapshot/second cadence
TAIL_BYTES = 512 * 1024   # bounded restart bootstrap; never scan the full growing log
TICK_FIELDS = ["utc_time", "provider_ts", "symbol", "bid", "ask",
               "bid_size", "ask_size", "mid", "spread"]

_buffers = defaultdict(lambda: deque(maxlen=BUFFER_MAXLEN))
_buffer_lock = threading.Lock()


def _parse_row(r):
    try:
        ts = datetime.fromisoformat(r["utc_time"]).timestamp()
        mid = float(r["mid"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "t": ts,
        "mid": mid,
        "bid_size": float(r.get("bid_size") or 0),
        "ask_size": float(r.get("ask_size") or 0),
    }


def record_ticks(rows):
    """Append newly captured tick rows to the process-local rolling window.

    Called by the standing tick logger after it persists each batch. The buffer
    is an acceleration layer only; tick_log.csv remains the append-only source
    of truth for research and replay.
    """
    parsed = []
    for r in rows:
        item = _parse_row(r)
        symbol = str(r.get("symbol") or "").upper()
        if item is not None and symbol:
            parsed.append((symbol, item))
    if not parsed:
        return
    with _buffer_lock:
        for symbol, item in parsed:
            _buffers[symbol].append(item)


def _clear_buffer(symbol=None):
    """Test helper; production code never clears the rolling window."""
    with _buffer_lock:
        if symbol is None:
            _buffers.clear()
        else:
            _buffers.pop(symbol.upper(), None)


def _buffer_window(symbol, cutoff):
    with _buffer_lock:
        return [dict(r) for r in _buffers.get(symbol.upper(), ()) if r["t"] >= cutoff]


def _tail_window(symbol, cutoff):
    """Read only a bounded tail of the CSV to bootstrap after a restart."""
    if not TICK_LOG.exists():
        return []
    try:
        with TICK_LOG.open("rb") as f:
            size = f.seek(0, 2)
            start = max(0, size - TAIL_BYTES)
            f.seek(start)
            blob = f.read()
    except OSError:
        return []
    if start:
        split = blob.split(b"\n", 1)
        blob = split[1] if len(split) == 2 else b""
    lines = blob.decode("utf-8", errors="ignore").splitlines()
    if not lines:
        return []
    reader = csv.DictReader(lines) if start == 0 else csv.DictReader(lines, fieldnames=TICK_FIELDS)
    rows = []
    for raw in reader:
        if raw.get("symbol") != symbol.upper():
            continue
        item = _parse_row(raw)
        if item is not None and item["t"] >= cutoff:
            rows.append(item)
    return rows


def _load_window(symbol, window_seconds=WINDOW_SECONDS):
    cutoff = datetime.now(timezone.utc).timestamp() - window_seconds
    buffered = _buffer_window(symbol, cutoff)
    if len(buffered) >= MIN_TICKS:
        return buffered

    # During the first seconds after a restart, merge a small file tail with the
    # live buffer. Once MIN_TICKS are in memory this path is no longer touched.
    merged = {}
    for r in _tail_window(symbol, cutoff) + buffered:
        merged[(r["t"], r["mid"], r["bid_size"], r["ask_size"])] = r
    return sorted(merged.values(), key=lambda r: r["t"])


def _ols(rows):
    n = len(rows)
    t0 = rows[0]["t"]
    ts = [r["t"] - t0 for r in rows]   # seconds elapsed, first tick = 0
    ps = [r["mid"] for r in rows]
    mean_t, mean_p = sum(ts) / n, sum(ps) / n
    var_t = sum((t - mean_t) ** 2 for t in ts)
    cov_tp = sum((t - mean_t) * (p - mean_p) for t, p in zip(ts, ps))
    slope = cov_tp / var_t if var_t else 0.0
    intercept = mean_p - slope * mean_t
    ss_tot = sum((p - mean_p) ** 2 for p in ps)
    ss_res = sum((p - (intercept + slope * t)) ** 2 for t, p in zip(ts, ps))
    r2 = (1 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    stdev_p = math.sqrt(sum((p - mean_p) ** 2 for p in ps) / n)
    return slope, r2, mean_p, stdev_p


def compute_read(symbol="SPY", window_seconds=WINDOW_SECONDS):
    rows = _load_window(symbol, window_seconds)
    n = len(rows)
    if n < MIN_TICKS:
        return {
            "available": False, "n": n, "min": MIN_TICKS,
            "reason": (f"Only {n} tick{'s' if n != 1 else ''} logged for {symbol.upper()} in the "
                       f"last {window_seconds}s — need at least {MIN_TICKS}. Keep the server "
                       f"running through market hours and this fills in within a minute or two."),
        }

    slope, r2, mean_p, stdev_p = _ols(rows)
    last_p = rows[-1]["mid"]
    slope_per_min = slope * 60
    slope_bps_per_min = (slope_per_min / mean_p * 10000) if mean_p else 0.0
    z = (last_p - mean_p) / stdev_p if stdev_p else 0.0

    flows = [(r["bid_size"] - r["ask_size"]) / (r["bid_size"] + r["ask_size"])
             for r in rows if (r["bid_size"] + r["ask_size"]) > 0]
    imbalance = sum(flows) / len(flows) if flows else 0.0

    if r2 < R2_FLOOR:
        lean, why = "choppy", f"R² is {r2:.2f} — the fit barely explains the scatter, not a real trend"
    elif slope_bps_per_min > 0.5:
        lean, why = "up", f"drifting up ~{slope_bps_per_min:.2f} bps/min, R² {r2:.2f}"
    elif slope_bps_per_min < -0.5:
        lean, why = "down", f"drifting down ~{slope_bps_per_min:.2f} bps/min, R² {r2:.2f}"
    else:
        lean, why = "flat", f"drift under 0.5 bps/min, R² {r2:.2f}"

    flow_agrees = None
    if lean in ("up", "down"):
        flow_dir = "up" if imbalance > 0.1 else "down" if imbalance < -0.1 else None
        flow_agrees = (flow_dir == lean) if flow_dir else None

    return {
        "available": True, "n": n, "window_seconds": window_seconds,
        "symbol": symbol.upper(), "lean": lean, "why": why,
        "slope_bps_per_min": round(slope_bps_per_min, 3), "r_squared": round(r2, 3),
        "quote_size_imbalance": round(imbalance, 3), "z_score": round(z, 2),
        "flow_agrees_with_trend": flow_agrees,
        "disclaimer": ("A line fit to the recent tape, not a forecast — SPY at this horizon is "
                       "close to a random walk, and this hasn't been checked against what actually "
                       "happened afterward. Describes the last two minutes; doesn't predict the next."),
    }
