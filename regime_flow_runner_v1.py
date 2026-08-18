"""Deterministic, fail-closed runner policy for PAPER guards.

The policy is intentionally pure: callers supply the latest regime and options-flow
evidence, and the Guard consumes only the resulting state. This keeps analytics off
the quote/exit loop and gives every policy transition a reproducible reason code.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math

import regime_layer_v0


RUNNER_POLICY_VERSION = "regime-flow-runner-v1"
REGIME_SOURCE_VERSION = regime_layer_v0.REGIME_VERSION
REFRESH_INTERVAL_SECONDS = 30.0
MIN_FLOW_EVENTS = 3

DEFAULTS = {
    "enabled": False,
    "version": RUNNER_POLICY_VERSION,
    "activation_profit_pct": 20.0,
    "runner_tolerance_pct": 10.0,
    "profit_lock_pct": 10.0,
    "observation_ttl_sec": 90.0,
    "confirm_observations": 2,
}


def _now(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value, name, *, minimum=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric") from None
    if not math.isfinite(parsed) or parsed < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum:g}")
    return parsed


def normalize_config(raw):
    """Normalize a per-guard policy config; disabled is the safe default."""
    if raw in (None, False):
        return {**DEFAULTS, "valid": True}
    if not isinstance(raw, dict):
        return {**DEFAULTS, "valid": False, "error": "runner_policy must be an object"}
    cfg = {**DEFAULTS, **raw}
    try:
        if cfg.get("version") != RUNNER_POLICY_VERSION:
            raise ValueError(f"version must be {RUNNER_POLICY_VERSION}")
        cfg["enabled"] = bool(cfg.get("enabled"))
        cfg["activation_profit_pct"] = _number(
            cfg["activation_profit_pct"], "activation_profit_pct")
        cfg["runner_tolerance_pct"] = _number(
            cfg["runner_tolerance_pct"], "runner_tolerance_pct")
        cfg["profit_lock_pct"] = _number(cfg["profit_lock_pct"], "profit_lock_pct")
        cfg["observation_ttl_sec"] = _number(
            cfg["observation_ttl_sec"], "observation_ttl_sec", minimum=1.0)
        cfg["confirm_observations"] = int(cfg["confirm_observations"])
        if cfg["confirm_observations"] < 1:
            raise ValueError("confirm_observations must be >= 1")
        if cfg["profit_lock_pct"] > cfg["activation_profit_pct"]:
            raise ValueError("profit_lock_pct cannot exceed activation_profit_pct")
        if cfg["runner_tolerance_pct"] <= 0:
            raise ValueError("runner_tolerance_pct must be > 0")
    except (TypeError, ValueError) as exc:
        return {**DEFAULTS, "enabled": False, "valid": False, "error": str(exc)}
    return {**cfg, "valid": True}


def initial_state(policy):
    return {
        "version": RUNNER_POLICY_VERSION,
        "status": "DISABLED" if not policy.get("enabled") else "AWAITING_OBSERVATION",
        "reason_codes": (["RUNNER_DISABLED"] if not policy.get("enabled") else []),
        "qualified_streak": 0,
        "last_observation_id": None,
        "last_qualified_at": None,
        "last_observed_at": None,
        "effective_tolerance_pct": None,
    }


def _direction_for_position(position_direction):
    normalized = str(position_direction or "").upper()
    if normalized in {"CALL", "BULLISH"}:
        return "BULLISH", "TREND_UP"
    if normalized in {"PUT", "BEARISH"}:
        return "BEARISH", "TREND_DOWN"
    return None, None


def _source_age(source, now):
    stamp = _timestamp(source)
    if stamp is None:
        return None
    return (now - stamp).total_seconds()


def _source_signature(regime, flow):
    regime_meta = regime.get("metadata") if isinstance(regime, dict) else {}
    return "|".join(str(part or "") for part in (
        regime_meta.get("fit_end_time") if isinstance(regime_meta, dict) else None,
        regime.get("state") if isinstance(regime, dict) else None,
        flow.get("as_of") if isinstance(flow, dict) else None,
        flow.get("tier") if isinstance(flow, dict) else None,
        flow.get("direction") if isinstance(flow, dict) else None,
    ))


def flow_from_institutional_snapshot(snapshot):
    """Turn one UW five-minute snapshot into a strict runner-flow observation.

    `flow_direction_score` is already a bounded, deterministic aggregation of
    normalized provider events. A GREEN runner observation requires at least
    three captured events and a score of exactly +1 or -1, meaning every
    classified event in the window points the same way. Anything weaker is
    retained as evidence but cannot widen an execution trail.
    """
    if hasattr(snapshot, "model_dump"):
        snapshot = snapshot.model_dump(mode="json")
    raw = snapshot if isinstance(snapshot, dict) else {}
    try:
        score = float(raw.get("flow_direction_score"))
        event_count = int(raw.get("event_count"))
    except (TypeError, ValueError):
        score, event_count = None, 0
    available = raw.get("institutional_flow_status") == "AVAILABLE"
    direction = "BULLISH" if score == 1.0 else "BEARISH" if score == -1.0 else "MIXED"
    green = available and event_count >= MIN_FLOW_EVENTS and direction != "MIXED"
    return {
        "source": "institutional_flow_snapshot_v0",
        "as_of": raw.get("window_end"),
        "fresh": available,
        "tier": "GREEN" if green else "INSUFFICIENT",
        "direction": direction,
        "event_count": event_count,
        "flow_direction_score": score,
    }


def regime_from_deterministic_tag(tag, *, observed_at):
    """Adapt the measured v0.1 tag to the runner's freshness contract."""
    raw = dict(tag) if isinstance(tag, dict) else {}
    metadata = dict(raw.get("metadata") or {})
    metadata.update({
        "fit_end_time": observed_at,
        "source": "regime_layer_v0.classify_regime",
        "regime_version": raw.get("schema_version"),
        "as_of_completed_bucket": raw.get("as_of_completed_bucket"),
    })
    return {
        **raw,
        "available": (
            raw.get("schema_version") == REGIME_SOURCE_VERSION
            and raw.get("status") == "FRESH"
        ),
        "metadata": metadata,
    }


def _eligibility(policy, observation, position_direction, peak, now):
    expected_flow, expected_regime = _direction_for_position(position_direction)
    if expected_flow is None:
        return False, ["UNSUPPORTED_POSITION_DIRECTION"], None
    # A quote such as 1.20 / 1.00 can land infinitesimally below 20 in binary
    # floating point. Preserve the displayed activation boundary exactly.
    if peak + 1e-9 < policy["activation_profit_pct"]:
        return False, ["RUNNER_NOT_ACTIVATED"], None
    if not isinstance(observation, dict):
        return False, ["OBSERVATION_MISSING"], None
    regime = observation.get("regime")
    flow = observation.get("flow")
    if not isinstance(regime, dict) or not isinstance(flow, dict):
        return False, ["OBSERVATION_COMPONENT_MISSING"], None
    reasons = []
    if regime.get("schema_version") != REGIME_SOURCE_VERSION:
        reasons.append("REGIME_SOURCE_VERSION_MISMATCH")
    if regime.get("available") is not True or regime.get("status") != "FRESH":
        reasons.append("REGIME_NOT_FRESH")
    if regime.get("state") != expected_regime:
        reasons.append("REGIME_DIRECTION_MISMATCH")
    regime_meta = regime.get("metadata") or {}
    regime_age = _source_age(regime_meta.get("fit_end_time"), now)
    if regime_age is None or regime_age < 0 or regime_age > policy["observation_ttl_sec"]:
        reasons.append("REGIME_STALE_OR_UNTIMED")
    flow_age = _source_age(flow.get("as_of"), now)
    if flow.get("fresh") is not True or flow_age is None or flow_age < 0 or flow_age > policy["observation_ttl_sec"]:
        reasons.append("FLOW_STALE_OR_UNTIMED")
    if flow.get("tier") != "GREEN":
        reasons.append("FLOW_NOT_GREEN")
    if flow.get("direction") != expected_flow:
        reasons.append("FLOW_DIRECTION_MISMATCH")
    return not reasons, reasons, _source_signature(regime, flow)


def apply_observation(policy, prior_state, observation, position_direction, peak, *, now=None):
    """Update the runner state from one point-in-time regime/flow observation."""
    current = _now(now)
    state = dict(prior_state or initial_state(policy))
    state["last_observed_at"] = current.isoformat()
    if not policy.get("valid"):
        state.update({"status": "CONFIG_INVALID", "reason_codes": ["CONFIG_INVALID"],
                      "qualified_streak": 0, "effective_tolerance_pct": None})
        return state
    if not policy.get("enabled"):
        state.update({"status": "DISABLED", "reason_codes": ["RUNNER_DISABLED"],
                      "qualified_streak": 0, "effective_tolerance_pct": None})
        return state
    eligible, reasons, source_id = _eligibility(
        policy, observation, position_direction, float(peak), current)
    if not eligible:
        state.update({"status": "BASELINE", "reason_codes": reasons,
                      "qualified_streak": 0, "effective_tolerance_pct": None})
        return state
    if source_id != state.get("last_observation_id"):
        state["qualified_streak"] = int(state.get("qualified_streak") or 0) + 1
        state["last_observation_id"] = source_id
    state["last_qualified_at"] = current.isoformat()
    if state["qualified_streak"] < policy["confirm_observations"]:
        state.update({"status": "AWAITING_CONFIRMATION",
                      "reason_codes": ["QUALIFIED_OBSERVATION_AWAITING_CONFIRMATION"],
                      "effective_tolerance_pct": None})
        return state
    state.update({"status": "ACTIVE", "reason_codes": ["REGIME_AND_FLOW_ALIGNED"],
                  "effective_tolerance_pct": policy["runner_tolerance_pct"]})
    return state


def effective_tolerance(policy, state, baseline_tolerance, peak, *, now=None):
    """Return the runner allowance only while its confirmed observation is fresh."""
    baseline = float(baseline_tolerance)
    if not policy.get("enabled") or state.get("status") != "ACTIVE":
        return baseline
    current = _now(now)
    qualified_age = _source_age(state.get("last_qualified_at"), current)
    if qualified_age is None or qualified_age < 0 or qualified_age > policy["observation_ttl_sec"]:
        return baseline
    # The runner can only widen a baseline trail after activation, and it can
    # never give back more than the pre-registered protected-profit amount.
    room_above_lock = max(0.0, float(peak) - policy["profit_lock_pct"])
    runner = min(policy["runner_tolerance_pct"], room_above_lock)
    return max(baseline, runner)


def snapshot(policy, state, baseline_tolerance, peak, *, now=None):
    effective = effective_tolerance(policy, state, baseline_tolerance, peak, now=now)
    return {
        "version": RUNNER_POLICY_VERSION,
        "enabled": bool(policy.get("enabled")),
        "valid": bool(policy.get("valid")),
        "status": state.get("status"),
        "reason_codes": list(state.get("reason_codes") or []),
        "qualified_streak": int(state.get("qualified_streak") or 0),
        "confirm_observations": policy.get("confirm_observations"),
        "activation_profit_pct": policy.get("activation_profit_pct"),
        "profit_lock_pct": policy.get("profit_lock_pct"),
        "baseline_tolerance_pct": round(float(baseline_tolerance), 6),
        "effective_tolerance_pct": round(float(effective), 6),
        "last_observed_at": state.get("last_observed_at"),
        "last_qualified_at": state.get("last_qualified_at"),
        "error": policy.get("error"),
    }
