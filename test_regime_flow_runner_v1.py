from datetime import datetime, timedelta, timezone

import regime_flow_runner_v1 as runner


NOW = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)


def _observation(*, regime_state="TREND_UP", flow_direction="BULLISH", age_seconds=5,
                 regime_status="FRESH", regime_version=runner.REGIME_SOURCE_VERSION,
                 tier="GREEN"):
    as_of = NOW - timedelta(seconds=age_seconds)
    return {
        "regime": {
            "schema_version": regime_version,
            "available": regime_status == "FRESH",
            "status": regime_status,
            "state": regime_state,
            "metadata": {"fit_end_time": as_of.isoformat()},
        },
        "flow": {"as_of": as_of.isoformat(), "fresh": True, "tier": tier,
                 "direction": flow_direction},
    }


def _next(observation):
    copied = {**observation, "regime": {**observation["regime"]}, "flow": {**observation["flow"]}}
    advanced = NOW + timedelta(seconds=30)
    copied["regime"]["metadata"] = {"fit_end_time": advanced.isoformat()}
    copied["flow"]["as_of"] = advanced.isoformat()
    return copied


def test_disabled_policy_never_changes_baseline():
    policy = runner.normalize_config(None)
    state = runner.apply_observation(policy, runner.initial_state(policy), _observation(), "CALL", 25, now=NOW)
    assert state["status"] == "DISABLED"
    assert runner.effective_tolerance(policy, state, 6, 25, now=NOW) == 6


def test_institutional_snapshot_requires_three_unanimous_events_for_green_flow():
    snapshot = {
        "institutional_flow_status": "AVAILABLE",
        "window_end": NOW.isoformat(),
        "event_count": 3,
        "flow_direction_score": 1.0,
    }
    flow = runner.flow_from_institutional_snapshot(snapshot)
    assert flow["tier"] == "GREEN" and flow["direction"] == "BULLISH"
    assert runner.flow_from_institutional_snapshot({**snapshot, "event_count": 2})["tier"] == "INSUFFICIENT"
    assert runner.flow_from_institutional_snapshot({**snapshot, "flow_direction_score": 0.5})["tier"] == "INSUFFICIENT"


def test_aligned_rolling_observations_activate_runner_and_preserve_profit_lock():
    policy = runner.normalize_config({"enabled": True})
    state = runner.apply_observation(policy, runner.initial_state(policy), _observation(), "CALL", 20, now=NOW)
    assert state["status"] == "AWAITING_CONFIRMATION"
    later = NOW + timedelta(seconds=30)
    state = runner.apply_observation(policy, state, _next(_observation()), "CALL", 20, now=later)
    assert state["status"] == "ACTIVE"
    assert runner.effective_tolerance(policy, state, 6, 20, now=later) == 10
    assert runner.effective_tolerance(policy, state, 12, 20, now=later) == 12


def test_conflicting_or_unavailable_evidence_immediately_reverts_to_baseline():
    policy = runner.normalize_config({"enabled": True, "confirm_observations": 1})
    state = runner.apply_observation(policy, runner.initial_state(policy), _observation(), "CALL", 25, now=NOW)
    assert state["status"] == "ACTIVE"
    state = runner.apply_observation(
        policy, state, _observation(flow_direction="BEARISH"), "CALL", 25,
        now=NOW + timedelta(seconds=30))
    assert state["status"] == "BASELINE"
    assert "FLOW_DIRECTION_MISMATCH" in state["reason_codes"]
    assert runner.effective_tolerance(policy, state, 6, 25, now=NOW) == 6
    state = runner.apply_observation(
        policy, state, _observation(regime_status="UNAVAILABLE"), "CALL", 25,
        now=NOW + timedelta(seconds=60))
    assert "REGIME_NOT_FRESH" in state["reason_codes"]
    state = runner.apply_observation(
        policy, state, _observation(regime_version="legacy-hmm-v0"), "CALL", 25,
        now=NOW + timedelta(seconds=90))
    assert "REGIME_SOURCE_VERSION_MISMATCH" in state["reason_codes"]


def test_stale_observation_and_expired_active_state_fail_closed_to_baseline():
    policy = runner.normalize_config({"enabled": True, "confirm_observations": 1})
    stale = _observation(age_seconds=120)
    state = runner.apply_observation(policy, runner.initial_state(policy), stale, "CALL", 25, now=NOW)
    assert state["status"] == "BASELINE"
    assert "FLOW_STALE_OR_UNTIMED" in state["reason_codes"]
    active = runner.apply_observation(policy, runner.initial_state(policy), _observation(), "CALL", 25, now=NOW)
    assert active["status"] == "ACTIVE"
    assert runner.effective_tolerance(policy, active, 6, 25, now=NOW + timedelta(seconds=91)) == 6
