#!/usr/bin/env python3
"""Deterministic provisional present-conditions composite for dashboard display."""

from __future__ import annotations

from typing import Any


FORMULA_VERSION = "market-context-index-display-v0"
WEIGHTS = {
    "price_structure": 0.40,
    "participation": 0.30,
    "cross_asset": 0.25,
    "options_structure": 0.05,
}
SECTOR_KEYS = (
    "xlk_session_return_pct", "xlf_session_return_pct", "xle_session_return_pct",
    "xlv_session_return_pct", "xly_session_return_pct", "xlp_session_return_pct",
    "xli_session_return_pct", "xlu_session_return_pct", "xlb_session_return_pct",
    "xlre_session_return_pct", "xlc_session_return_pct",
)


def _envelope(record: dict[str, Any], group: str, key: str) -> dict[str, Any]:
    value = (((record.get("fields") or {}).get(group) or {}).get(key) or {})
    return value if isinstance(value, dict) else {}


def _available(record: dict[str, Any], group: str, key: str) -> Any:
    envelope = _envelope(record, group, key)
    if envelope.get("status") != "AVAILABLE":
        return None
    if str(envelope.get("data_freshness") or "").upper() not in {"CLEAN", "DEGRADED"}:
        return None
    return envelope.get("value")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _sign(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    return 1.0 if number > 0 else -1.0 if number < 0 else 0.0


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def compute_context_index(record: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        "formula_version": FORMULA_VERSION,
        "is_inferential": False,
        "label": "exploratory composite · not a probability · not a signal · present conditions, not a forecast.",
        "weights": WEIGHTS,
    }
    if not isinstance(record, dict):
        return {**base, "status": "NOT_SCORED", "reason": "NO_RECORD", "score": None}
    freshness = str(record.get("data_freshness") or "STALE").upper()
    if freshness not in {"CLEAN", "DEGRADED"}:
        return {**base, "status": "NOT_SCORED", "reason": "RECORD_NOT_FRESH", "score": None}

    vwap_sign = _sign(_available(record, "spy_structure", "vwap_distance_bps"))
    opening = _available(record, "spy_structure", "opening_range_30m_state")
    opening_lean = {"ABOVE": 1.0, "INSIDE": 0.0, "BELOW": -1.0}.get(str(opening).upper())
    breadth = _number(_available(record, "largecap_breadth", "largecap_breadth_proxy"))
    qqq = _sign(_available(record, "cross_asset", "qqq_session_return_pct"))
    iwm = _sign(_available(record, "cross_asset", "iwm_session_return_pct"))
    sectors = [_sign(_available(record, "cross_asset", key)) for key in SECTOR_KEYS]
    gamma_regime = _available(record, "options_structure", "gamma_regime")
    required = [vwap_sign, opening_lean, breadth, qqq, iwm, gamma_regime, *sectors]
    if any(value is None for value in required):
        return {**base, "status": "NOT_SCORED", "reason": "GROUP_INPUT_MISSING", "score": None}

    sector_lean = sum(sectors) / len(sectors)
    leans = {
        "price_structure": (vwap_sign + opening_lean) / 2.0,
        "participation": _clamp((breadth - 0.5) / 0.5),
        "cross_asset": (qqq + iwm + sector_lean) / 3.0,
        # Gamma changes path risk, not direction. A zero lean makes it a low-weight
        # neutral/dampening allocation and prevents gamma alone moving the score.
        "options_structure": 0.0,
    }
    weighted_lean = sum(WEIGHTS[name] * lean for name, lean in leans.items())
    score = round(_clamp(weighted_lean) * 50.0 + 50.0)
    regime = str(gamma_regime).lower()
    modifier = "BREAKOUT RISK — DIRECTION UNKNOWN" if regime == "negative" else "DAMPENING — DIRECTION UNKNOWN"
    return {
        **base,
        "group_leans": {name: round(value, 6) for name, value in leans.items()},
        "options_modifier": modifier,
        "score": int(score),
        "status": "SCORED",
        "weighted_lean": round(weighted_lean, 6),
    }
