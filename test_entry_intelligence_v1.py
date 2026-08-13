"""Offline safety and reproducibility tests for Entry Intelligence v1."""

import json
import hashlib
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import entry_bid_capture_v1 as bids
import entry_cohort_lock_v1 as locks
import entry_episode_research_v1 as research
import entry_intelligence_v1 as ei
import entry_bid_collector_v1 as collector
import entry_contract_data_v1 as contract_data
import decision_replay_v1 as replay
import feature_engine as fe
import scalpr_config


NOW = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)


@contextmanager
def raises(exc_type):
    try:
        yield
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__}")


def rules(side="CALL"):
    return {
        "atr_period": 14, "rsi_period": 14,
        "extension_lookback_bars": 12, "extension_atr_multiple": 1.5,
        "rsi_threshold": 35.0 if side == "CALL" else 65.0,
        "round_number_increment": 1.0, "level_proximity_pct": 0.15,
    }


def execution_rules():
    return {
        "max_quote_age_seconds": 5.0, "max_spread_pct": 6.0,
        "delta_min": 0.4, "delta_max": 0.5, "delta_target": 0.45,
        "min_contract_volume": 200, "min_open_interest": 500,
        "max_ask_price": 5.0, "dte_min": 0, "dte_max": 2,
    }


def cost_model():
    return {
        "cost_model_version": "entry-intel-cost-model-v1",
        "return_convention": "decimal_fraction",
        "commission_per_contract_usd": 0.0,
        "regulatory_fees_round_trip_per_contract_usd": 0.12,
        "slippage_ticks_per_side_primary": 1,
        "slippage_sensitivity_ticks_per_side": [0, 1, 2],
        "tick_size_usd_per_share": 0.01,
        "spread_handling": "already_captured_by_ask_entry_bid_exit_do_not_readd",
    }


def minute_bars(side="CALL"):
    """One observation per 5m bucket is sufficient for deterministic OHLC tests."""
    rows = []
    for bucket in range(13):
        if side == "CALL":
            close = 100.0 - bucket * 0.7
        else:
            close = 90.0 + bucket * 0.7
        rows.append({"t": bucket * 5, "open": close + (0.2 if side == "CALL" else -0.2),
                     "high": close + 0.6, "low": close - 0.6,
                     "close": close, "volume": 1000})
    if side == "CALL":
        rows.extend([
            {"t": 65, "open": 91.0, "high": 91.3, "low": 89.0, "close": 90.0, "volume": 1000},
            {"t": 70, "open": 91.1, "high": 91.2, "low": 90.0, "close": 90.4, "volume": 1000},
            {"t": 75, "open": 91.5, "high": 91.6, "low": 91.1, "close": 91.3, "volume": 1000},
        ])
    else:
        rows.extend([
            {"t": 65, "open": 99.0, "high": 101.0, "low": 98.7, "close": 100.0, "volume": 1000},
            {"t": 70, "open": 99.9, "high": 100.0, "low": 98.8, "close": 99.6, "volume": 1000},
            {"t": 75, "open": 98.5, "high": 98.9, "low": 98.4, "close": 98.7, "volume": 1000},
        ])
    return rows


def test_unavailable_axis_cannot_fabricate_zero():
    for state in ("STALE", "MISSING", "UNAVAILABLE", "UNUSABLE"):
        with raises(ValueError):
            ei.EvidenceAxis(value=0.0, status=state)
    axis = ei.EvidenceAxis(value=None, status="UNAVAILABLE", unavailable=["quote"])
    assert axis.value is None and axis.is_calibrated_probability is False


def test_decision_shape_is_conditional():
    unavailable = ei.EvidenceAxis(value=None, status="UNAVAILABLE", unavailable=["option_quote"])
    scores = ei.EvidenceScoreBlock(direction=unavailable, quality=unavailable, executability=unavailable)
    packet = ei.DecisionPacket(
        decision_id="d", cohort_id="draft", rules_hash="r", label_version="l",
        config_version="c", config_hash="h", observed_at=NOW, decided_at=NOW,
        symbol="SPY", setup_type="x", decision="NO_TRADE", scores=scores,
        missing_or_stale=["option_quote"],
    )
    assert packet.selected_contract is None and packet.frozen_target is None
    with raises(ValueError):
        ei.DecisionPacket(
            decision_id="bad", cohort_id="draft", rules_hash="r", label_version="l",
            config_version="c", config_hash="h", observed_at=NOW, decided_at=NOW,
            symbol="SPY", setup_type="x", decision="CALL", scores=scores,
        )


def test_low_and_high_mechanics_are_true_mirrors():
    low = ei.evaluate_reversal_setup(
        symbol="SPY", side="CALL", minute_bars=minute_bars("CALL"), now_minute=80,
        context={"prior_day_low": 91.3, "premarket_low": 90.0, "session_vwap": 93.0},
        rules=rules("CALL"))
    high = ei.evaluate_reversal_setup(
        symbol="SPY", side="PUT", minute_bars=minute_bars("PUT"), now_minute=80,
        context={"prior_day_high": 98.7, "premarket_high": 100.0, "session_vwap": 97.0},
        rules=rules("PUT"))
    assert low["status"] == high["status"] == "FRESH"
    assert low["qualified"] is True and high["qualified"] is True
    assert low["passed"] == high["passed"] == [
        "atr_extension", "level_proximity", "momentum_slowing", "causal_confirmation"]


def test_contract_selection_is_point_in_time_and_deterministic():
    base = {
        "option_type": "CALL", "expiry": "2026-08-07", "dte": 2, "strike": 775,
        "bid": 1.90, "ask": 2.00, "volume": 300, "open_interest": 800,
        "quote_observed_at": (NOW - timedelta(seconds=1)).isoformat(),
    }
    contracts = [
        {**base, "option_symbol": "B", "delta": 0.46},
        {**base, "option_symbol": "A", "delta": 0.44},
        {**base, "option_symbol": "STALE", "delta": 0.45,
         "quote_observed_at": (NOW - timedelta(seconds=30)).isoformat()},
    ]
    result = ei.select_contract(side="CALL", contracts=contracts, decided_at=NOW,
                                rules=execution_rules())
    assert result["selected"]["option_symbol"] == "A"
    assert any(row["option_symbol"] == "STALE" and "quote_stale" in row["reasons"]
               for row in result["rejected"])


def test_contract_assembly_uses_chain_volume_and_vendor_delta_with_provenance():
    config = scalpr_config.load_entry_intelligence_config()["contract_assembly"]
    record = contract_data.assemble_contract(
        chain={
            "symbol": "SPY260807C00775000", "volume": 300,
            "oi": 800, "iv": 0.20,
        },
        chain_observed_at="2026-08-05T10:50:00-04:00",
        snapshot={
            "bid": 1.90, "ask": 2.00,
            "quote_observed_at": NOW - timedelta(seconds=1),
            "received_at": NOW, "delta": 0.45,
            "snapshot_volume": 0,
        },
        underlying={
            "spot": 775.0, "observed_at": NOW - timedelta(seconds=30),
            "received_at": NOW, "source": "alpaca_stock_bars:iex",
        },
        decided_at=NOW, config=config)
    assert record["volume"] == 300
    assert record["ignored_snapshot_volume"]["state"] == "MISSING"
    assert record["delta"] == 0.45
    assert record["delta_source"] == "alpaca_snapshot_greeks"
    assert record["field_provenance"]["volume"]["source"] == (
        "unusual_whales_option_chain")
    assert record["field_provenance"]["volume"]["state"] == "FRESH"
    for evidence in record["field_provenance"].values():
        for key in ("observed_at", "received_at"):
            if evidence.get(key):
                assert datetime.fromisoformat(evidence[key]) <= NOW


def test_same_day_local_bs_delta_supports_zero_dte_without_fabrication():
    config = scalpr_config.load_entry_intelligence_config()["contract_assembly"]
    decided = datetime(2026, 8, 7, 15, 0, tzinfo=timezone.utc)
    record = contract_data.assemble_contract(
        chain={
            "symbol": "SPY260807C00775000", "volume": 1000,
            "oi": 1500, "iv": 0.20,
        },
        chain_observed_at="2026-08-07T10:30:00-04:00",
        snapshot={
            "bid": 1.90, "ask": 2.00,
            "quote_observed_at": decided - timedelta(seconds=1),
            "received_at": decided, "delta": None,
            "snapshot_volume": 0,
        },
        underlying={
            "spot": 774.2, "observed_at": decided - timedelta(seconds=30),
            "received_at": decided, "source": "alpaca_stock_bars:iex",
        },
        decided_at=decided, config=config)
    assert record["dte"] == 0
    assert record["delta_source"] == "local_black_scholes"
    assert 0.40 <= abs(record["delta"]) <= 0.50
    result = ei.select_contract(
        side="CALL", contracts=[record], decided_at=decided,
        rules=execution_rules())
    assert result["selected"]["option_symbol"] == "SPY260807C00775000"
    assert result["selected"]["contract_selection_version"] == (
        "entry-execution-contract-selection-v2")


def test_contract_assembly_keeps_missing_chain_volume_unavailable():
    config = scalpr_config.load_entry_intelligence_config()["contract_assembly"]
    record = contract_data.assemble_contract(
        chain={"symbol": "SPY260807C00775000", "oi": 800, "iv": 0.20},
        chain_observed_at="2026-08-05T10:50:00-04:00",
        snapshot={
            "bid": 1.90, "ask": 2.00,
            "quote_observed_at": NOW - timedelta(seconds=1),
            "received_at": NOW, "delta": 0.45, "snapshot_volume": 999,
        },
        underlying={
            "spot": 775.0, "observed_at": NOW - timedelta(seconds=30),
            "received_at": NOW,
        },
        decided_at=NOW, config=config)
    assert record["volume"] is None
    assert record["field_provenance"]["volume"]["state"] == "MISSING"
    result = ei.select_contract(
        side="CALL", contracts=[record], decided_at=NOW,
        rules=execution_rules())
    assert "volume_unavailable" in result["rejected"][0]["reasons"]


def test_contract_assembly_rejects_future_timestamps_without_lookahead():
    config = scalpr_config.load_entry_intelligence_config()["contract_assembly"]
    record = contract_data.assemble_contract(
        chain={
            "symbol": "SPY260807C00775000", "volume": 300,
            "oi": 800, "iv": 0.20,
        },
        chain_observed_at=NOW + timedelta(seconds=1),
        snapshot={
            "bid": 1.90, "ask": 2.00,
            "quote_observed_at": NOW + timedelta(seconds=1),
            "received_at": NOW, "delta": 0.45, "snapshot_volume": 0,
        },
        underlying={
            "spot": 775.0, "observed_at": NOW + timedelta(seconds=1),
            "received_at": NOW,
        },
        decided_at=NOW, config=config)
    assert record["bid"] is None and record["volume"] is None
    assert record["delta"] is None
    assert record["field_provenance"]["bid"]["state"] == "UNUSABLE"
    assert record["field_provenance"]["volume"]["state"] == "UNUSABLE"
    for evidence in record["field_provenance"].values():
        for key in ("observed_at", "received_at"):
            if evidence.get(key):
                assert datetime.fromisoformat(evidence[key]) <= NOW


def test_packet_has_separate_axes_and_native_bid_levels():
    with open("frozen_cohort_low_reversal_v1.json") as handle:
        cohort = json.load(handle)
    setup = ei.evaluate_reversal_setup(
        symbol="SPY", side="CALL", minute_bars=minute_bars("CALL"), now_minute=80,
        context={"prior_day_low": 91.3, "premarket_low": 90.0, "session_vwap": 93.0},
        rules=rules("CALL"))
    contract = {
        "option_symbol": "SPY-C", "option_type": "CALL", "expiry": "2026-08-07",
        "dte": 2, "strike": 775, "delta": 0.45, "bid": 1.9, "ask": 2.0,
        "spread_pct": 5.128, "volume": 300, "open_interest": 800,
        "quote_observed_at": NOW, "quote_received_at": NOW,
    }
    packet = ei.build_decision_packet(
        cohort_document=cohort, setup=setup,
        contract_selection={"selected": contract, "rejected": [], "source_status": "FRESH"},
        observed_at=NOW, decided_at=NOW, config_version="test", config_hash="hash")
    assert packet.decision == "CALL"
    assert packet.frozen_invalidation.price == 1.6 and packet.frozen_target.price == 2.6
    assert not hasattr(packet.scores, "aggregate")
    assert packet.scores.direction.is_calibrated_probability is False


def test_bid_path_is_ask_entry_bid_exit_and_collision_is_conservative():
    decision = {
        "decision_id": "d1", "cohort_id": "draft", "option_symbol": "SPY-C",
        "entry_quote_observed_at": NOW.isoformat(), "entry_ask": 1.0,
        "target_bid": 1.3, "stop_bid": 0.8, "outcome_horizon_minutes": 60,
        "bid_poll_interval_seconds": 5, "max_fresh_gap_seconds": 15,
        "minimum_coverage_fraction": 0.8, "return_horizons_minutes": [5, 15, 30, 60],
        "cost_model": cost_model(),
        "config_version": "test", "config_hash": "hash",
    }
    r1 = bids.build_bid_record(
        decision_id="d1", cohort_id="draft", option_symbol="SPY-C",
        observed_at=NOW + timedelta(seconds=5), received_at=NOW + timedelta(seconds=5),
        bid=1.1, ask=1.12, max_quote_age_seconds=5,
        config_version="test", config_hash="hash")
    r2 = bids.build_bid_record(
        decision_id="d1", cohort_id="draft", option_symbol="SPY-C",
        observed_at=NOW + timedelta(seconds=10), received_at=NOW + timedelta(seconds=10),
        bid=1.31, ask=1.33, max_quote_age_seconds=5,
        config_version="test", config_hash="hash")
    out = bids.evaluate_outcome(decision, [r1, r2], evaluated_at=NOW + timedelta(seconds=10))
    assert out["status"] == "FINAL" and out["first_hit"] == "TARGET_FIRST"
    assert out["entry_basis"] == "EXECUTABLE_ASK" and out["exit_path_basis"] == "EXECUTABLE_BID"
    assert out["entry_fill_classification"] == "SIMULATED"
    assert out["broker_fill_observed"] is False
    assert out["cost_model_status"] == "IMPLEMENTED_RESEARCH_MODEL"
    assert out["net_return_fraction"] == 0.28594059
    assert out["net_return_fraction_by_slippage_ticks_per_side"] == {
        "0": 0.3088, "1": 0.28594059, "2": 0.26352941,
    }

    # Same provider timestamp with contradictory records retains the worst bid.
    stop = bids.build_bid_record(
        decision_id="d1", cohort_id="draft", option_symbol="SPY-C",
        observed_at=NOW + timedelta(seconds=10), received_at=NOW + timedelta(seconds=10),
        bid=0.79, ask=0.81, max_quote_age_seconds=5,
        config_version="test", config_hash="hash", provider_sequence="correction")
    conservative = bids.evaluate_outcome(decision, [r1, r2, stop], evaluated_at=NOW + timedelta(seconds=10))
    assert conservative["first_hit"] == "STOP_FIRST"


def test_cost_model_matches_frozen_example_and_floors_negative_proceeds():
    result = bids.net_return_sensitivity(
        entry_ask=1.50, exit_bid=1.80, cost_model=cost_model())
    assert result["net_return_fraction_by_slippage_ticks_per_side"] == {
        "0": 0.1992, "1": 0.18463576, "2": 0.17026316,
    }
    assert result["net_return_fraction"] == 0.18463576
    floor = bids.net_return_sensitivity(
        entry_ask=0.01, exit_bid=0.0, cost_model=cost_model())
    assert floor["net_return_fraction_by_slippage_ticks_per_side"]["2"] == -1.04


def test_cost_model_fails_closed_on_spread_double_count_or_missing_version():
    with raises(ValueError):
        bids.validate_cost_model({})
    with raises(ValueError):
        bids.validate_cost_model({**cost_model(), "spread_handling": "add_spread_again"})


def test_unavailable_bid_is_recorded_not_imputed():
    row = bids.build_bid_record(
        decision_id="d", cohort_id="draft", option_symbol="X",
        observed_at=NOW, received_at=NOW + timedelta(seconds=20),
        bid=None, ask=1.0, max_quote_age_seconds=5,
        config_version="test", config_hash="hash")
    assert row["status"] == "MISSING" and row["bid"] is None
    assert "NO_EXECUTABLE_BID" in row["unavailable_reasons"]


def test_small_provider_ahead_skew_is_treated_as_fresh():
    row = bids.build_bid_record(
        decision_id="d", cohort_id="draft", option_symbol="X",
        observed_at=NOW + timedelta(seconds=2),
        received_at=NOW, bid=1.0, ask=1.02, max_quote_age_seconds=5,
        config_version="test", config_hash="hash")
    assert row["status"] == "FRESH"
    assert row["clock_skew_tolerance_seconds"] == 3.0
    assert row["quote_age_seconds"] == -2.0


def test_episode_admission_blocks_duplicate_and_overlap():
    candidate = {"episode_key": "e1", "decision_id": "d1", "cohort_id": "c",
                 "symbol": "SPY", "side": "CALL", "session_date": "2026-08-05",
                 "decided_at": NOW.isoformat(),
                 "config_version": "test", "config_hash": "hash"}
    first = research.admit_episode(candidate, existing=[], outcome_horizon_minutes=60, cooldown_minutes=60)
    duplicate = research.admit_episode(candidate, existing=[first], outcome_horizon_minutes=60, cooldown_minutes=60)
    overlap = research.admit_episode(
        {**candidate, "episode_key": "e2", "decision_id": "d2",
         "decided_at": (NOW + timedelta(minutes=30)).isoformat()},
        existing=[first], outcome_horizon_minutes=60, cooldown_minutes=60)
    assert first["admitted"] is True
    assert duplicate["rejection_reason"] == "DUPLICATE_REVERSAL_REFERENCE"
    assert overlap["rejection_reason"] == "OVERLAPPING_OUTCOME_OR_COOLDOWN"
    assert first["config_version"] == "test" and first["config_hash"] == "hash"


def test_trade_and_rejection_share_reference_identity_and_admission():
    with open("frozen_cohort_low_reversal_v1.json") as handle:
        cohort = json.load(handle)
    setup = ei.evaluate_reversal_setup(
        symbol="SPY", side="CALL", minute_bars=minute_bars("CALL"), now_minute=80,
        context={"prior_day_low": 91.3, "premarket_low": 90.0,
                 "session_vwap": 93.0}, rules=rules("CALL"))
    unavailable = {
        "selected": None, "rejected": [], "source_status": "MISSING",
        "reason": "execution_not_evaluated_episode_admission_pending"}
    rejected = ei.build_decision_packet(
        cohort_document=cohort, setup={**setup, "qualified": False,
                                      "failed": ["momentum_slowing"]},
        contract_selection=unavailable, observed_at=NOW, decided_at=NOW,
        config_version="test", config_hash="hash")
    selected = {
        "option_symbol": "SPY-C", "option_type": "CALL",
        "expiry": "2026-08-07", "dte": 2, "strike": 775,
        "delta": 0.45, "bid": 1.9, "ask": 2.0, "spread_pct": 5.128,
        "volume": 300, "open_interest": 800,
        "quote_observed_at": NOW, "quote_received_at": NOW}
    taken = ei.build_decision_packet(
        cohort_document=cohort, setup=setup,
        contract_selection={"selected": selected, "rejected": [],
                            "source_status": "FRESH"},
        observed_at=NOW, decided_at=NOW,
        config_version="test", config_hash="hash")
    assert rejected.episode_key == taken.episode_key
    assert rejected.decision_id == taken.decision_id
    first = research.admit_episode({
        "episode_key": rejected.episode_key, "decision_id": rejected.decision_id,
        "cohort_id": rejected.cohort_id, "symbol": "SPY", "side": "CALL",
        "session_date": "2026-08-05", "decided_at": NOW.isoformat(),
        "config_version": "test", "config_hash": "hash",
        "episode_kind": "REVERSAL_CANDIDATE"}, existing=[],
        outcome_horizon_minutes=60, cooldown_minutes=60)
    second = research.admit_episode({
        "episode_key": taken.episode_key, "decision_id": taken.decision_id,
        "cohort_id": taken.cohort_id, "symbol": "SPY", "side": "CALL",
        "session_date": "2026-08-05", "decided_at": NOW.isoformat(),
        "config_version": "test", "config_hash": "hash",
        "episode_kind": "REVERSAL_CANDIDATE"}, existing=[first],
        outcome_horizon_minutes=60, cooldown_minutes=60)
    assert first["admitted"] is True
    assert second["rejection_reason"] == "DUPLICATE_REVERSAL_REFERENCE"


def test_no_trade_plan_gate_records_and_manifest_are_auditable():
    config = scalpr_config.load_entry_intelligence_config()
    with open("frozen_cohort_low_reversal_v1.json") as handle:
        cohort = json.load(handle)
    setup = ei.evaluate_reversal_setup(
        symbol="SPY", side="CALL", minute_bars=minute_bars("CALL"), now_minute=80,
        context={"prior_day_low": 91.3, "premarket_low": 90.0,
                 "session_vwap": 93.0}, rules=rules("CALL"))
    setup = {**setup, "qualified": False, "failed": ["momentum_slowing"],
             "passed": ["atr_extension", "level_proximity", "causal_confirmation"]}
    selected = {
        "option_symbol": "SPY-C", "option_type": "CALL",
        "expiry": "2026-08-07", "dte": 2, "strike": 775,
        "delta": 0.45, "bid": 1.90, "ask": 2.00, "spread_pct": 5.128,
        "volume": 300, "open_interest": 800,
        "quote_observed_at": NOW, "quote_received_at": NOW}
    selection = {"selected": selected, "rejected": [],
                 "source_status": "FRESH"}
    packet = ei.build_decision_packet(
        cohort_document=cohort, setup=setup, contract_selection=selection,
        observed_at=NOW, decided_at=NOW,
        config_version=config["config_version"], config_hash=config["config_hash"])
    payload = packet.model_dump(mode="json")
    plan = replay.build_no_trade_tracking_plan(
        packet=payload, side="CALL", selection=selection,
        config=config, created_at=NOW)
    assert plan.status == "TRACKABLE" and plan.entry_ask == 2.0
    assert plan.stop_bid == 1.6 and plan.target_bid == 2.6
    assert plan.realized_trade is False and plan.portfolio_pnl_eligible is False
    unavailable_plan = replay.build_no_trade_tracking_plan(
        packet=payload, side="CALL",
        selection={"selected": None, "source_status": "UNAVAILABLE",
                   "reason": "provider_unavailable"},
        config=config, created_at=NOW)
    assert unavailable_plan.status == "UNTRACKABLE"
    assert unavailable_plan.selected_contract is None
    assert unavailable_plan.reason_code == "PROVIDER_UNAVAILABLE"

    gates = replay.build_gate_results(
        packet=payload, setup=setup, selection=selection,
        config=config, evaluated_at=NOW)
    assert [gate.result for gate in gates[:4]] == ["PASS", "PASS", "FAIL", "PASS"]
    assert gates[-1].result == "PASS" and gates[-1].reason_code == "CONTRACT_SELECTED"
    assert gates[-1].gate_version == "entry-execution-gates-v1"
    assert gates[-1].threshold_or_config_hash
    assert gates[-1].source_ref.endswith("#CONTRACT_SELECTION")
    not_evaluated = replay.build_gate_results(
        packet=payload, setup=setup,
        selection={"selected": None, "source_status": "MISSING",
                   "reason": "execution_not_evaluated_direction_failed"},
        config=config, evaluated_at=NOW)[-1]
    unavailable = replay.build_gate_results(
        packet=payload, setup=setup,
        selection={"selected": None, "source_status": "UNAVAILABLE",
                   "reason": "provider_unavailable"},
        config=config, evaluated_at=NOW)[-1]
    assert not_evaluated.result == "NOT_EVALUATED"
    assert unavailable.result == "UNAVAILABLE"
    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "sources.jsonl"
        manifests = Path(td) / "manifests.jsonl"
        manifest = replay.write_evidence_manifest(
            packet=payload, setup=setup, selection=selection, config=config,
            source_path=source, manifest_path=manifests, created_at=NOW)
        verified = replay.verify_evidence_manifest(manifest, source, payload)
        assert verified["status"] == "VERIFIED"
        rows = list(fe._iter_jsonl(source))
        rows[0]["content"]["qualified"] = True
        source.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                          encoding="utf-8")
        failed = replay.verify_evidence_manifest(manifest, source)
        assert failed["status"] == "UNRECOVERABLE"
        assert failed["current_data_fallback_used"] is False
        wrong_decision = replay.verify_evidence_manifest(
            manifest, source, {**payload, "symbol": "QQQ"})
        assert wrong_decision["status"] == "UNRECOVERABLE"


def test_hypothetical_outcomes_are_physically_and_semantically_separate():
    config = scalpr_config.load_entry_intelligence_config()
    plan = replay.NoTradeTrackingPlan(
        plan_id="p", decision_id="d", cohort_id="c", episode_key="e",
        side="CALL", status="TRACKABLE",
        reason_code="COUNTERFACTUAL_CONTRACT_FROZEN_AT_DECISION",
        config_version=config["config_version"], config_hash=config["config_hash"],
        created_at=NOW,
        selected_contract={"option_symbol": "SPY-C"},
        entry_quote_observed_at=NOW, entry_ask=1.0,
        stop_bid=0.8, target_bid=1.3, outcome_horizon_minutes=60,
        bid_poll_interval_seconds=5, max_fresh_gap_seconds=15,
        minimum_coverage_fraction=0.8,
        return_horizons_minutes=[5, 15, 30, 60],
        cost_model=config["outcome"]["cost_model"])
    decorated = replay.decorate_hypothetical_outcome({
        "decision_id": "d", "status": "FINAL", "evaluated_at": NOW.isoformat()},
        plan)
    assert decorated["outcome_kind"] == "HYPOTHETICAL_NO_TRADE"
    assert decorated["portfolio_pnl_eligible"] is False
    with tempfile.TemporaryDirectory() as td:
        hypothetical = Path(td) / "hypothetical.jsonl"
        real = Path(td) / "real.jsonl"
        assert replay.append_hypothetical_outcome(decorated, hypothetical) is True
        assert hypothetical.exists() and not real.exists()


def test_collector_is_default_off_and_read_only():
    assert scalpr_config.entry_bid_capture_enabled({}) is False
    assert scalpr_config.entry_bid_capture_enabled(
        {"ENTRY_INTEL_BID_CAPTURE_ENABLED": "0"}) is False
    assert scalpr_config.entry_bid_capture_enabled(
        {"ENTRY_INTEL_BID_CAPTURE_ENABLED": "1"}) is True
    status = collector.disabled_status()
    assert status["state"] == "DISABLED_DEFAULT_OFF"
    assert status["execution_authority"] is False and status["guard_access"] is False
    source = Path(collector.__file__).read_text(encoding="utf-8")
    replay_source = Path(replay.__file__).read_text(encoding="utf-8")
    forbidden = ("alpaca.trading", "submit_order(", "MarketOrderRequest(",
                 "LimitOrderRequest(", "import scalp_server", "from scalp_server")
    assert not any(pattern in source for pattern in forbidden)
    assert not any(pattern in replay_source for pattern in forbidden)


def test_collector_without_flag_writes_zero_capture_records():
    old = os.environ.pop("ENTRY_INTEL_BID_CAPTURE_ENABLED", None)
    paths = [Path(name) for name in (
        "entry_intelligence_decisions_v1.jsonl",
        "entry_intelligence_episodes_v1.jsonl",
        "entry_intelligence_bid_ticks_v1.jsonl",
        "entry_intelligence_outcomes_v1.jsonl",
        "entry_intelligence_gate_results_v1.jsonl",
        "entry_intelligence_evidence_sources_v1.jsonl",
        "entry_intelligence_evidence_manifests_v1.jsonl",
        "entry_intelligence_decision_events_v1.jsonl",
        "entry_intelligence_no_trade_plans_v1.jsonl",
        "entry_intelligence_hypothetical_bid_ticks_v1.jsonl",
        "entry_intelligence_hypothetical_outcomes_v1.jsonl")]
    before = {path: (path.exists(), path.stat().st_mtime_ns if path.exists() else None)
              for path in paths}
    try:
        with raises(RuntimeError):
            collector.EntryBidCollector(stock_data=None, option_data=None, feed="iex")
    finally:
        if old is not None:
            os.environ["ENTRY_INTEL_BID_CAPTURE_ENABLED"] = old
    after = {path: (path.exists(), path.stat().st_mtime_ns if path.exists() else None)
             for path in paths}
    assert after == before


def test_enabled_collector_arms_closed_without_market_capture():
    old_flag = os.environ.get("ENTRY_INTEL_BID_CAPTURE_ENABLED")
    old_root = collector.ROOT
    try:
        with tempfile.TemporaryDirectory() as td:
            collector.ROOT = Path(td)
            os.environ["ENTRY_INTEL_BID_CAPTURE_ENABLED"] = "1"
            service = collector.EntryBidCollector(
                stock_data=None, option_data=None, feed="iex")
            status = service.tick(
                market_active=False, session_open_et=None, session_close_et=None,
                now=datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc))
            assert status["state"] == "ARMED_MARKET_CLOSED"
            assert status["enabled"] is True and status["market_window"] is False
            assert not any((Path(td) / name).exists() for name in (
                "entry_intelligence_decisions_v1.jsonl",
                "entry_intelligence_episodes_v1.jsonl",
                "entry_intelligence_bid_ticks_v1.jsonl",
                "entry_intelligence_outcomes_v1.jsonl",
                "entry_intelligence_gate_results_v1.jsonl",
                "entry_intelligence_evidence_sources_v1.jsonl",
                "entry_intelligence_evidence_manifests_v1.jsonl",
                "entry_intelligence_decision_events_v1.jsonl",
                "entry_intelligence_no_trade_plans_v1.jsonl",
                "entry_intelligence_hypothetical_bid_ticks_v1.jsonl",
                "entry_intelligence_hypothetical_outcomes_v1.jsonl"))
            service.replay_writer.close()
    finally:
        collector.ROOT = old_root
        if old_flag is None:
            os.environ.pop("ENTRY_INTEL_BID_CAPTURE_ENABLED", None)
        else:
            os.environ["ENTRY_INTEL_BID_CAPTURE_ENABLED"] = old_flag


def test_collector_refreshes_snapshot_before_contract_selection():
    class Obj:
        def __init__(self, **values):
            self.__dict__.update(values)

    class FakeOptionData:
        def get_option_snapshot(self, request):
            assert request.feed.value == "opra"
            symbol = request.symbol_or_symbols[0]
            return {symbol: Obj(
                latest_quote=Obj(
                    timestamp=NOW - timedelta(seconds=1),
                    bid_price=1.90, ask_price=2.00),
                greeks=Obj(delta=0.45),
                daily_bar=Obj(volume=0))}

    old_flag = os.environ.get("ENTRY_INTEL_BID_CAPTURE_ENABLED")
    old_root = collector.ROOT
    try:
        with tempfile.TemporaryDirectory() as td:
            collector.ROOT = Path(td)
            (Path(td) / "runs").mkdir()
            (Path(td) / "runs/SPY_2026-08-05.json").write_text(json.dumps({
                "as_of": "2026-08-05T10:59:00-04:00",
                "contracts": [{
                    "symbol": "SPY260807C00775000", "type": "C",
                    "expiry": "2026-08-07", "dte": 2, "strike": 775,
                    "oi": 800, "volume": 300, "iv": 0.20,
                    "bid": 0.5, "ask": 0.6,
                }],
            }), encoding="utf-8")
            os.environ["ENTRY_INTEL_BID_CAPTURE_ENABLED"] = "1"
            service = collector.EntryBidCollector(
                stock_data=None, option_data=FakeOptionData(), feed="iex")
            selected = service._live_contracts(
                side="CALL", market_date="2026-08-05", received_at=NOW)
            assert selected["source_status"] == "FRESH"
            assert selected["selected"]["option_symbol"] == "SPY260807C00775000"
            assert selected["selected"]["delta"] == 0.45
            assert selected["selected"]["volume"] == 300
            assert selected["selected"]["field_provenance"]["volume"][
                "source"] == "unusual_whales_option_chain"
            service.replay_writer.close()
    finally:
        collector.ROOT = old_root
        if old_flag is None:
            os.environ.pop("ENTRY_INTEL_BID_CAPTURE_ENABLED", None)
        else:
            os.environ["ENTRY_INTEL_BID_CAPTURE_ENABLED"] = old_flag


def test_collector_admits_before_poll_and_splits_real_from_hypothetical():
    call_setup = ei.evaluate_reversal_setup(
        symbol="SPY", side="CALL", minute_bars=minute_bars("CALL"), now_minute=80,
        context={"prior_day_low": 91.3, "premarket_low": 90.0,
                 "session_vwap": 93.0}, rules=rules("CALL"))
    call_setup = {
        **call_setup, "qualified": False, "failed": ["momentum_slowing"],
        "passed": ["atr_extension", "level_proximity", "causal_confirmation"]}
    put_setup = ei.evaluate_reversal_setup(
        symbol="SPY", side="PUT", minute_bars=minute_bars("PUT"), now_minute=80,
        context={"prior_day_high": 98.7, "premarket_high": 100.0,
                 "session_vwap": 97.0}, rules=rules("PUT"))
    old_flag = os.environ.get("ENTRY_INTEL_BID_CAPTURE_ENABLED")
    old_root = collector.ROOT
    original_evaluate = collector.intelligence.evaluate_reversal_setup
    try:
        with tempfile.TemporaryDirectory() as td:
            collector.ROOT = Path(td)
            os.environ["ENTRY_INTEL_BID_CAPTURE_ENABLED"] = "1"
            service = collector.EntryBidCollector(
                stock_data=None, option_data=None, feed="iex")
            service._underlying_inputs = lambda **kwargs: ([], {})
            collector.intelligence.evaluate_reversal_setup = (
                lambda **kwargs: call_setup if kwargs["side"] == "CALL" else put_setup)

            def selected_after_admission(*, side, market_date, received_at,
                                         underlying_spot=None,
                                         underlying_observed_at=None,
                                         underlying_received_at=None):
                admitted = [row for row in fe._iter_jsonl(service.episode_path)
                            if row.get("side") == side and row.get("admitted")]
                assert admitted, "contract polling occurred before episode admission"
                return {"selected": {
                    "option_symbol": f"SPY-{side}", "option_type": side,
                    "expiry": "2026-08-07", "dte": 2, "strike": 775,
                    "delta": 0.45, "bid": 1.90, "ask": 2.00,
                    "spread_pct": 5.128, "volume": 300, "open_interest": 800,
                    "quote_observed_at": NOW, "quote_received_at": NOW,
                }, "rejected": [], "eligible_count": 1,
                    "source_status": "FRESH"}

            service._live_contracts = selected_after_admission
            opened = NOW - timedelta(minutes=80)
            service._evaluate_minute(
                session_open_et=opened,
                session_close_et=opened + timedelta(hours=6, minutes=30),
                now=NOW)
            service.replay_writer.flush()
            decisions = list(fe._iter_jsonl(service.decision_path))
            assert {row["decision"] for row in decisions} == {"NO_TRADE", "PUT"}
            assert len(list(fe._iter_jsonl(service.evidence_manifest_path))) == 2
            assert len(list(fe._iter_jsonl(service.decision_event_path))) == 2
            assert len(list(fe._iter_jsonl(service.gate_result_path))) == 10
            plans = list(fe._iter_jsonl(service.no_trade_plan_path))
            assert len(plans) == 1 and plans[0]["status"] == "TRACKABLE"
            assert len(service.active_hypothetical) == 1
            assert len(service.active) == 1
            assert service.no_trade_bid_path.exists()
            assert service.bid_path.exists()
            service.replay_writer.close()
    finally:
        collector.intelligence.evaluate_reversal_setup = original_evaluate
        collector.ROOT = old_root
        if old_flag is None:
            os.environ.pop("ENTRY_INTEL_BID_CAPTURE_ENABLED", None)
        else:
            os.environ["ENTRY_INTEL_BID_CAPTURE_ENABLED"] = old_flag


def test_approved_config_matches_both_unlocked_drafts():
    config = scalpr_config.load_entry_intelligence_config()
    assert config["operator_approval"]["approved"] is True
    assert config["enabled_default"] is False
    assert config["execution"]["max_spread_pct"] == 6.0
    assert config["execution"]["max_ask_price"] == 5.0
    assert config["outcome"]["option_risk_fraction"] == 0.20
    assert config["outcome"]["cost_model_status"] == "IMPLEMENTED_RESEARCH_MODEL"
    for side, path in (("CALL", "frozen_cohort_low_reversal_v1.json"),
                       ("PUT", "frozen_cohort_high_reversal_v1.json")):
        with open(path) as handle:
            document = json.load(handle)
        collector._validate_draft_against_config(document, config, side)


def test_replay_persistence_is_bounded_background_work():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "queued.jsonl"
        writer = replay.BoundedReplayPersistence(max_pending=2)
        writer.submit(fe._atomic_append, path, {"ok": True})
        writer.flush()
        assert list(fe._iter_jsonl(path)) == [{"ok": True}]
        assert writer.pending == 0
        writer.close()


def test_session_block_null_has_plus_one_and_is_deterministic():
    episodes = [{"session_date": f"2026-07-{i:02d}", "decided_at": NOW.isoformat(),
                 "incremental_return": 0.05} for i in range(1, 21)]
    a = research.session_block_sign_null(episodes, n_permutations=999, block_sessions=1, seed=7)
    b = research.session_block_sign_null(episodes, n_permutations=999, block_sessions=1, seed=7)
    assert a == b and a["finite_sample_plus_one"] is True
    assert a["one_sided_p_value"] >= 1 / 1000


def test_draft_hashes_reproduce_and_lock_fails_closed():
    for path in ("frozen_cohort_low_reversal_v1.json", "frozen_cohort_high_reversal_v1.json"):
        with open(path) as handle:
            document = json.load(handle)
        assert document["draft_hash"] == fe.canonical_hash(document["proposed"])
        verdict = locks.validate_lock_candidate(document, now=NOW)
        assert verdict["valid"] is False
        assert "operator_not_confirmed" in verdict["problems"]


def flat_proof(observed_at=NOW, **changes):
    proof = {
        "schema_version": locks.ACCOUNT_FLAT_PROOF_VERSION,
        "source": "alpaca_trading_api_direct_uncached",
        "observed_at_utc": observed_at.isoformat(),
        "mode": "paper",
        "account_status": "ACTIVE",
        "account_identity_sha256": "a" * 64,
        "positions_count": 0,
        "open_orders_count": 0,
        "flat": True,
    }
    proof.update(changes)
    return proof


def calendar_proof(target_session="2026-08-06", observed_at=NOW, **changes):
    proof = {
        "schema_version": locks.MARKET_CALENDAR_PROOF_VERSION,
        "source": "alpaca_trading_calendar_direct_uncached",
        "market": "XNYS",
        "target_session": target_session,
        "observed_at_utc": observed_at.isoformat(),
        "is_trading_day": True,
        "market_open_utc": "2026-08-06T13:30:00+00:00",
        "market_close_utc": "2026-08-06T20:00:00+00:00",
    }
    proof.update(changes)
    return proof


def disk_usage(free=16, total=100):
    return lambda _path: SimpleNamespace(total=total, used=total-free, free=free)


def feed_sanity_reference(root, *, config_hash, **report_changes):
    report = {
        "schema_version": locks.FEED_SANITY_EVIDENCE_VERSION,
        "session_date": "2026-08-05",
        "mode": "PAPER_SHADOW_ONLY",
        "verdict": "PASS",
        "operator_confirmed": True,
        "config_hash": config_hash,
        "checks": {field: "PASS" for field in (
            "trackable_plans", "eligible_contract_selection", "executable_bid_capture",
            "outcomes_0_ticks", "outcomes_1_tick", "outcomes_2_ticks",
            "direction_post_warmup", "gross_bar_quote_gaps", "session_continuity",
        )},
        "counts": {
            "trackable_plans": 2, "eligible_contracts": 2,
            "executable_bid_records": 40,
            "outcome_records_0_ticks": 2, "outcome_records_1_tick": 2,
            "outcome_records_2_ticks": 2,
            "post_warmup_direction_evaluations": 200,
            "post_warmup_direction_fresh_fraction": 0.95,
            "late_guard_cycles": 0, "connection_reset_errors": 0,
            "gross_gap_count": 0,
        },
    }
    report.update(report_changes)
    path = Path(root) / "rth_feed_sanity.json"
    path.write_text(json.dumps(report, sort_keys=True) + "\n")
    return {
        "evidence_path": path.name,
        "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_lock_requires_fresh_direct_flat_paper_account_proof():
    base = flat_proof()
    assert locks.validate_account_flat_proof(base, now=NOW) == []
    cases = (
        (None, "account_flat_proof_missing"),
        (flat_proof(source="cached"), "account_flat_proof_not_direct_uncached"),
        (flat_proof(mode="live"), "account_flat_proof_not_paper"),
        (flat_proof(positions_count=1, flat=False), "account_positions_nonzero"),
        (flat_proof(open_orders_count=1, flat=False), "account_open_orders_nonzero"),
        (flat_proof(observed_at=NOW - timedelta(seconds=6)), "account_flat_proof_stale"),
    )
    for proof, expected in cases:
        assert expected in locks.validate_account_flat_proof(proof, now=NOW)


def test_lock_recomputes_hashes_and_rejects_any_mismatch():
    with open("frozen_cohort_low_reversal_v1.json") as handle:
        candidate = json.load(handle)
    candidate["proposed"]["implementation_hashes"] = (
        locks.expected_implementation_hashes(candidate))
    assert locks.validate_implementation_hashes(candidate) == []
    candidate["proposed"]["implementation_hashes"]["collector_hash"] = "0" * 64
    assert "collector_hash_mismatch" in locks.validate_implementation_hashes(candidate)
    candidate["proposed"]["implementation_hashes"] = (
        locks.expected_implementation_hashes(candidate))
    candidate["proposed"]["implementation_hashes"]["mystery_hash"] = "a" * 64
    assert "mystery_hash_unverifiable" in locks.validate_implementation_hashes(candidate)


def test_calendar_disk_and_feed_sanity_proofs_fail_closed():
    with open("frozen_cohort_low_reversal_v1.json") as handle:
        candidate = json.load(handle)
    candidate.update({"document_status": "READY_TO_LOCK", "operator_confirmed": True,
                      "operator_confirmation": "offline-test", "unconfirmed_fields": []})
    candidate["proposed"]["target_session"] = "2026-08-06"
    candidate["proposed"]["implementation_hashes"] = (
        locks.expected_implementation_hashes(candidate))
    config_hash = candidate["proposed"]["implementation_hashes"]["config_hash"]
    with tempfile.TemporaryDirectory() as td:
        evidence = feed_sanity_reference(td, config_hash=config_hash)
        common = dict(
            now=NOW, account_flat_proof=flat_proof(),
            market_calendar_proof=calendar_proof(), feed_sanity_evidence=evidence,
            evidence_root=td,
        )
        assert locks.validate_lock_candidate(
            candidate, disk_usage_fn=disk_usage(), **common)["valid"] is True
        low = locks.validate_lock_candidate(
            candidate, disk_usage_fn=disk_usage(free=14), **common)
        assert "disk_headroom_below_15_percent" in low["problems"]
        holiday = locks.validate_lock_candidate(
            candidate, disk_usage_fn=disk_usage(),
            **{**common, "market_calendar_proof": calendar_proof(is_trading_day=False)})
        assert "target_session_not_trading_day" in holiday["problems"]
        tampered = dict(evidence, evidence_sha256="f" * 64)
        bad_feed = locks.validate_lock_candidate(
            candidate, disk_usage_fn=disk_usage(),
            **{**common, "feed_sanity_evidence": tampered})
        assert "feed_sanity_evidence_hash_mismatch" in bad_feed["problems"]


def test_synthetic_ready_lock_is_append_only_and_preopen():
    with tempfile.TemporaryDirectory() as td:
        with open("frozen_cohort_low_reversal_v1.json") as handle:
            candidate = json.load(handle)
        candidate.update({"document_status": "READY_TO_LOCK", "operator_confirmed": True,
                          "operator_confirmation": "offline-test", "unconfirmed_fields": []})
        candidate["proposed"]["cohort_id"] = "test-cohort"
        candidate["proposed"]["target_session"] = "2026-08-06"
        candidate["proposed"]["implementation_hashes"] = (
            locks.expected_implementation_hashes(candidate))
        config_hash = candidate["proposed"]["implementation_hashes"]["config_hash"]
        proof = flat_proof()
        evidence = feed_sanity_reference(td, config_hash=config_hash)
        record = locks.build_lock_record(
            candidate, now=NOW, account_flat_proof=proof,
            market_calendar_proof=calendar_proof(), feed_sanity_evidence=evidence,
            evidence_root=td, disk_usage_fn=disk_usage())
        assert record["frozen"]["account_flat_proof"] == proof
        assert record["frozen"]["market_calendar_proof"]["is_trading_day"] is True
        assert record["frozen"]["feed_sanity_evidence"] == evidence
        assert record["frozen"]["disk_headroom_proof"]["passes"] is True
        path = Path(td) / "locks.jsonl"
        assert locks.append_lock(record, path) is True
        assert locks.append_lock(record, path) is False
        changed = {**record, "frozen_hash": "different"}
        with raises(ValueError):
            locks.append_lock(changed, path)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print("ALL ENTRY INTELLIGENCE V1 TESTS PASSED")
