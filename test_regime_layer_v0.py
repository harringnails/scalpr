import math

import regime_layer_v0 as regime


def _minute_bar(bucket, close, spread=0.5):
    return {
        "t": bucket * 5,
        "open": close,
        "high": close + spread,
        "low": close - spread,
        "close": close,
        "volume": 1000,
    }


def _history(count=78):
    rows = []
    close = 100.0
    for bucket in range(count):
        close += 0.10 + (0.02 if bucket % 3 == 0 else 0.0)
        rows.append(_minute_bar(bucket, close, spread=0.3 + bucket * 0.002))
    return rows


def test_efficiency_ratio_is_one_for_pure_trend_and_zero_for_zigzag():
    trend = [100.0 + index for index in range(21)]
    zigzag = [100.0 if index % 2 == 0 else 101.0 for index in range(21)]

    assert math.isclose(regime.efficiency_ratio(trend), 1.0)
    assert math.isclose(regime.efficiency_ratio(zigzag), 0.0)


def test_atr_percentile_is_monotonic_in_current_atr():
    prior = [float(value) for value in range(1, 61)]
    percentiles = [regime.atr_percentile(current, prior) for current in (5, 30, 55)]

    assert percentiles == sorted(percentiles)
    assert percentiles[0] < percentiles[1] < percentiles[2]


def test_missing_input_returns_unknown_not_tradeable_default():
    tag = regime.classify_regime(minute_bars=[], now_minute=0)

    assert tag["state"] == "UNKNOWN"
    assert tag["status"] == "UNAVAILABLE"
    assert tag["advisory_only"] is True
    assert tag["admission_gate"] is False
    assert tag["conditioning_verdict"] is False
    assert tag["filter_deployed"] is False
    assert tag["execution_authority"] is False


def test_regime_at_t0_is_unchanged_by_future_bars():
    bars = _history()
    now_minute = 74 * 5
    at_t0 = regime.classify_regime(minute_bars=bars[:74], now_minute=now_minute)
    with_future = regime.classify_regime(minute_bars=bars, now_minute=now_minute)

    assert at_t0["state"] != "UNKNOWN"
    assert with_future == at_t0


def test_cross_session_atr_tail_makes_regime_available_at_bar_21():
    current = _history(21)
    prior_session = _history(78)

    without_tail = regime.classify_regime(
        minute_bars=current, now_minute=21 * 5)
    with_tail = regime.classify_regime(
        minute_bars=current, now_minute=21 * 5,
        prior_session_minute_bars=[prior_session])

    assert without_tail["state"] == "UNKNOWN"
    assert without_tail["missing_reasons"] == ["prior_atr_observations<60"]
    assert with_tail["state"] != "UNKNOWN"
    assert with_tail["prior_atr_observation_count"] == regime.ATR_WINDOW
    assert with_tail["atr_percentile_history_source"] == (
        "rolling_cross_session_completed_intraday_atr_tail")


def test_cross_session_percentile_matches_direct_carried_tail_computation():
    current = _history(23)
    prior_session = _history(78)
    tag = regime.classify_regime(
        minute_bars=current, now_minute=23 * 5,
        prior_session_minute_bars=[prior_session])

    completed_current = regime.completed_five_minute_bars(current, 23 * 5)
    reference = regime.prior_atr_tail([prior_session])
    reference.extend(regime.atr_observations(completed_current[:-1]))
    reference = reference[-regime.ATR_WINDOW:]
    current_atr = regime.wave_atr.compute_intraday_atr(
        completed_current, period=regime.ATR_PERIOD, warmup_min=regime.ATR_PERIOD,
        full=regime.ATR_PERIOD, as_of=completed_current[-1]["bucket"],
    )["atr_value"]

    assert tag["atr_percentile"] == round(
        regime.atr_percentile(current_atr, reference), 10)


def test_insufficient_cross_session_history_remains_unknown():
    tag = regime.classify_regime(
        minute_bars=_history(21), now_minute=21 * 5,
        prior_session_minute_bars=[_history(20)],
    )

    assert tag["state"] == "UNKNOWN"
    assert tag["missing_reasons"] == ["prior_atr_observations<60"]


def test_cross_session_regime_at_t0_ignores_future_current_session_bars():
    current = _history(30)
    prior_session = _history(78)
    at_t0 = regime.classify_regime(
        minute_bars=current[:21], now_minute=21 * 5,
        prior_session_minute_bars=[prior_session])
    with_future = regime.classify_regime(
        minute_bars=current, now_minute=21 * 5,
        prior_session_minute_bars=[prior_session])

    assert at_t0["state"] != "UNKNOWN"
    assert with_future == at_t0


def test_warmup_fix_keeps_every_pre_registered_threshold_unchanged():
    assert regime.REGIME_VERSION == "deterministic-regime-layer-v0.1"
    assert (regime.ER_LOOKBACK, regime.ER_TREND, regime.ER_RANGE) == (20, 0.5, 0.3)
    assert (regime.ATR_PERIOD, regime.ATR_WINDOW, regime.ATR_HIGH_PERCENTILE) == (14, 60, 0.80)
