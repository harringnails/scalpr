"""Deterministic, point-in-time advisory regime tags for A2 episodes."""

from __future__ import annotations

import math
from typing import Iterable

from entry_intelligence_v1 import completed_five_minute_bars
import wave_atr


REGIME_VERSION = "deterministic-regime-layer-v0.1"
THRESHOLD_PROVENANCE = (
    "CONVENTIONAL_FIRST_PRINCIPLES_PRE_REGISTERED_REGIME_LAYER_SPEC_V0_"
    "ATR_PERCENTILE_WINDOW_SCOPE_CORRECTION_V0_1"
)
ER_LOOKBACK = 20
ER_TREND = 0.5
ER_RANGE = 0.3
ATR_PERIOD = 14
ATR_WINDOW = 60
ATR_HIGH_PERCENTILE = 0.80
REGIME_STATES = {
    "HIGH_VOL", "TREND_UP", "TREND_DOWN", "RANGE", "TRANSITIONAL", "UNKNOWN",
}


def efficiency_ratio(closes: Iterable[float], lookback: int = ER_LOOKBACK) -> float | None:
    """Kaufman ER over the final lookback changes, bounded to [0, 1]."""
    try:
        values = [float(value) for value in closes]
    except (TypeError, ValueError):
        return None
    if lookback <= 0 or len(values) < lookback + 1 or not all(map(math.isfinite, values)):
        return None
    window = values[-(lookback + 1):]
    path = sum(abs(current - previous) for previous, current in zip(window, window[1:]))
    if path == 0:
        return 0.0
    displacement = abs(window[-1] - window[0])
    return min(1.0, max(0.0, displacement / path))


def atr_percentile(current_atr: float, prior_atrs: Iterable[float]) -> float | None:
    """Empirical CDF rank of current ATR against the frozen prior window."""
    try:
        current = float(current_atr)
        prior = [float(value) for value in prior_atrs]
    except (TypeError, ValueError):
        return None
    if not prior or not math.isfinite(current) or not all(map(math.isfinite, prior)):
        return None
    return sum(value <= current for value in prior) / len(prior)


def unknown_regime(
    *, completed_bar_count: int, reasons: list[str], as_of_bucket=None,
    prior_atr_observation_count: int | None = None,
) -> dict:
    return {
        "schema_version": REGIME_VERSION,
        "state": "UNKNOWN",
        "status": "UNAVAILABLE",
        "missing_reasons": reasons,
        "efficiency_ratio": None,
        "atr_percentile": None,
        "direction": None,
        "current_atr": None,
        "completed_bar_count": completed_bar_count,
        "as_of_completed_bucket": as_of_bucket,
        "prior_atr_observation_count": prior_atr_observation_count,
        "atr_percentile_history_source": (
            "rolling_cross_session_completed_intraday_atr_tail"
        ),
        "parameters": {
            "er_lookback_bars": ER_LOOKBACK,
            "er_trend": ER_TREND,
            "er_range": ER_RANGE,
            "atr_period_bars": ATR_PERIOD,
            "atr_percentile_window": ATR_WINDOW,
            "atr_high_percentile": ATR_HIGH_PERCENTILE,
        },
        "threshold_provenance": THRESHOLD_PROVENANCE,
        "research_stage": "STAGE_0_1_MEASUREMENT_ONLY",
        "advisory_only": True,
        "admission_gate": False,
        "conditioning_verdict": False,
        "filter_deployed": False,
        "execution_authority": False,
    }


def atr_observations(completed_bars: list[dict]) -> list[float]:
    """Return causal ATR values for a single completed intraday session.

    Each value is calculated from that session only.  Callers may concatenate
    sessions afterwards, but never let an overnight price gap enter the ATR.
    """
    observations: list[float] = []
    for end in range(ATR_PERIOD, len(completed_bars) + 1):
        audit = wave_atr.compute_intraday_atr(
            completed_bars[:end], period=ATR_PERIOD, warmup_min=ATR_PERIOD,
            full=ATR_PERIOD, as_of=completed_bars[end - 1].get("bucket"),
        )
        value = audit.get("atr_value")
        if value is not None:
            observations.append(float(value))
    return observations


def prior_atr_tail(prior_session_minute_bars: Iterable[list[dict]] | None) -> list[float]:
    """Build the bounded, chronological historical ATR reference tail.

    The input is ordered oldest-to-newest and contains only prior sessions.
    Resampling each session independently preserves the intraday ATR contract.
    """
    observations: list[float] = []
    for minute_bars in prior_session_minute_bars or []:
        if not minute_bars:
            continue
        try:
            last_minute = max(int(row["t"]) for row in minute_bars)
            session_bars = completed_five_minute_bars(
                minute_bars, now_minute=last_minute + 1)
        except (KeyError, TypeError, ValueError):
            continue
        observations.extend(atr_observations(session_bars))
    return observations[-ATR_WINDOW:]


def classify_regime(
    *, minute_bars: list[dict], now_minute: int,
    prior_session_minute_bars: Iterable[list[dict]] | None = None,
) -> dict:
    """Classify at t0 from current ER plus a causal cross-session ATR tail."""
    try:
        bars = completed_five_minute_bars(minute_bars, now_minute)
    except (KeyError, TypeError, ValueError):
        return unknown_regime(completed_bar_count=0, reasons=["invalid_completed_5m_bars"])

    count = len(bars)
    as_of_bucket = bars[-1].get("bucket") if bars else None
    required_current_bars = max(ER_LOOKBACK + 1, ATR_PERIOD)
    if count < required_current_bars:
        return unknown_regime(
            completed_bar_count=count,
            reasons=[f"completed_5m_bars<{required_current_bars}"],
            as_of_bucket=as_of_bucket,
        )

    try:
        closes = [float(bar["close"]) for bar in bars]
        er = efficiency_ratio(closes, ER_LOOKBACK)
        direction_delta = closes[-1] - closes[-(ER_LOOKBACK + 1)]
        direction = 1 if direction_delta > 0 else -1 if direction_delta < 0 else 0
        # The current bar must not appear in its own percentile reference.
        prior_atrs = prior_atr_tail(prior_session_minute_bars)
        prior_atrs.extend(atr_observations(bars[:-1]))
        prior_atrs = prior_atrs[-ATR_WINDOW:]
        current_audit = wave_atr.compute_intraday_atr(
            bars, period=ATR_PERIOD, warmup_min=ATR_PERIOD,
            full=ATR_PERIOD, as_of=as_of_bucket,
        )
        current_atr = current_audit.get("atr_value")
        percentile = atr_percentile(current_atr, prior_atrs)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return unknown_regime(
            completed_bar_count=count,
            reasons=["missing_or_invalid_regime_input"],
            as_of_bucket=as_of_bucket,
            prior_atr_observation_count=len(prior_atrs) if "prior_atrs" in locals() else None,
        )
    if len(prior_atrs) < ATR_WINDOW:
        return unknown_regime(
            completed_bar_count=count,
            reasons=[f"prior_atr_observations<{ATR_WINDOW}"],
            as_of_bucket=as_of_bucket,
            prior_atr_observation_count=len(prior_atrs),
        )
    if er is None or percentile is None or current_atr is None:
        return unknown_regime(
            completed_bar_count=count,
            reasons=["missing_or_invalid_regime_input"],
            as_of_bucket=as_of_bucket,
            prior_atr_observation_count=len(prior_atrs),
        )

    if percentile > ATR_HIGH_PERCENTILE:
        state = "HIGH_VOL"
    elif er >= ER_TREND and direction > 0:
        state = "TREND_UP"
    elif er >= ER_TREND and direction < 0:
        state = "TREND_DOWN"
    elif er <= ER_RANGE:
        state = "RANGE"
    else:
        state = "TRANSITIONAL"
    return {
        "schema_version": REGIME_VERSION,
        "state": state,
        "status": "FRESH",
        "missing_reasons": [],
        "efficiency_ratio": round(er, 10),
        "atr_percentile": round(percentile, 10),
        "direction": direction,
        "current_atr": current_atr,
        "completed_bar_count": count,
        "as_of_completed_bucket": as_of_bucket,
        "prior_atr_observation_count": len(prior_atrs),
        "atr_percentile_history_source": (
            "rolling_cross_session_completed_intraday_atr_tail"
        ),
        "parameters": {
            "er_lookback_bars": ER_LOOKBACK,
            "er_trend": ER_TREND,
            "er_range": ER_RANGE,
            "atr_period_bars": ATR_PERIOD,
            "atr_percentile_window": ATR_WINDOW,
            "atr_high_percentile": ATR_HIGH_PERCENTILE,
        },
        "threshold_provenance": THRESHOLD_PROVENANCE,
        "research_stage": "STAGE_0_1_MEASUREMENT_ONLY",
        "advisory_only": True,
        "admission_gate": False,
        "conditioning_verdict": False,
        "filter_deployed": False,
        "execution_authority": False,
    }
