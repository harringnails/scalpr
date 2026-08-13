from datetime import datetime, timedelta, timezone

import a2_measurement as a2


BASE = datetime(2026, 8, 11, 14, 45, 3, tzinfo=timezone.utc)


def _episode(side="CALL", key="e1"):
    return {
        "episode_key": key,
        "episode_record_id": f"record-{key}",
        "decision_id": f"decision-{key}",
        "cohort_id": "low_reversal_v1" if side == "CALL" else "high_reversal_v1",
        "config_version": "entry-intelligence-config-v1.2.0",
        "config_hash": "frozen-config-hash",
        "symbol": "SPY",
        "side": side,
        "session_date": "2026-08-11",
        "decided_at": BASE.isoformat(),
        "admitted": True,
    }


def _quote(at, mid):
    return {
        "provider_ts": at,
        "received_at": at,
        "mid": mid,
        "bid": mid - 0.01,
        "ask": mid + 0.01,
        "source": "test",
    }


def _complete_quotes(prices):
    quotes = [_quote(BASE - timedelta(seconds=1), 100.0)]
    for horizon, price in prices.items():
        quotes.append(_quote(BASE + timedelta(minutes=horizon, seconds=1), price))
    return quotes


def test_label_episode_signs_call_returns_and_stamps_provenance():
    labeled = a2.label_episode(
        _episode(), session_quotes=_complete_quotes({5: 101.0, 15: 102.0, 30: 103.0, 60: 104.0})
    )
    assert labeled["label_status"] == "AVAILABLE"
    assert labeled["setup_direction_sign"] == 1
    assert labeled["signed_return_60m"] > 0
    assert labeled["config_hash"] == "frozen-config-hash"
    assert labeled["anchor_provider_ts"] < labeled["decided_at"]
    assert labeled["endpoint_provider_ts"]["60m"] is not None


def test_label_episode_signs_bearish_put_returns_positive():
    labeled = a2.label_episode(
        _episode("PUT"), session_quotes=_complete_quotes({5: 99.0, 15: 98.0, 30: 97.0, 60: 96.0})
    )
    assert labeled["label_status"] == "AVAILABLE"
    assert labeled["setup_direction_sign"] == -1
    assert labeled["signed_return_60m"] > 0


def test_missing_endpoint_is_unavailable_not_zero():
    labeled = a2.label_episode(
        _episode(), session_quotes=_complete_quotes({5: 101.0, 15: 102.0, 30: 103.0})
    )
    assert labeled["label_status"] == "UNAVAILABLE"
    assert labeled["signed_return_60m"] is None
    assert "missing_endpoint_60m_within_5s" in labeled["missing_reason"]


def test_anchor_never_uses_a_future_quote():
    quotes = _complete_quotes({5: 101.0, 15: 102.0, 30: 103.0, 60: 104.0})[1:]
    labeled = a2.label_episode(_episode(), session_quotes=quotes)
    assert labeled["label_status"] == "UNAVAILABLE"
    assert labeled["missing_reason"] == "missing_clean_anchor_within_5s"


def test_episode_key_deduplication_preserves_one_observation():
    unique, duplicates = a2.deduplicate_episodes([_episode(key="same"), _episode(key="same")])
    assert len(unique) == 1
    assert duplicates == 1


def test_cross_side_timestamp_collision_is_reported_as_an_integrity_failure():
    put = _episode("PUT", key="put")
    summary = a2.summarize_a2([], episodes=[_episode("CALL", key="call"), put])
    assert summary["n_cross_side_timestamp_collision_groups"] == 1
    assert summary["data_integrity_status"] == "FAIL_CROSS_SIDE_TIMESTAMP_COLLISIONS"
    assert summary["phase4_preflight"] == "BLOCKED_BY_CROSS_SIDE_COLLISIONS"


def test_measurement_summary_flags_underpowered_when_needed():
    summary = a2.summarize_a2([
        {"label_status": "AVAILABLE", "signed_return_60m": 0.01, "side": "CALL"}
    ])
    assert summary["power_gate_reached"] is False
    assert summary["phase4_preflight"] == "UNDERPOWERED_INCONCLUSIVE"


if __name__ == "__main__":
    test_label_episode_signs_call_returns_and_stamps_provenance()
    test_label_episode_signs_bearish_put_returns_positive()
    test_missing_endpoint_is_unavailable_not_zero()
    test_anchor_never_uses_a_future_quote()
    test_episode_key_deduplication_preserves_one_observation()
    test_cross_side_timestamp_collision_is_reported_as_an_integrity_failure()
    test_measurement_summary_flags_underpowered_when_needed()
    print("ALL A2 MEASUREMENT TESTS PASSED")
