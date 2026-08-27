"""Regression tests for decision-linked post-close ledger accounting."""

import post_close_audit as audit


def test_admissions_and_outcome_revisions_are_counted_honestly():
    decisions = [
        {"decision_id": "d1", "decided_at": "2026-08-13T15:00:00Z",
         "selected_contract": {"option_symbol": "SPY-C"},
         "scores": {"direction": {"status": "FRESH"}}},
        {"decision_id": "d2", "decided_at": "2026-08-13T16:00:00Z",
         "selected_contract": None,
         "scores": {"direction": {"status": "MISSING"}}},
    ]
    episodes = [
        {"episode_record_id": "e1", "decision_id": "d1", "admitted": True,
         "decided_at": "2026-08-13T15:00:00Z"},
        {"episode_record_id": "e2", "decision_id": "d2", "admitted": False,
         "rejection_reason": "DUPLICATE_REVERSAL_REFERENCE",
         "decided_at": "2026-08-13T16:00:00Z"},
    ]
    outcomes = [
        {"outcome_record_id": "o1", "decision_id": "d1", "status": "PENDING",
         "evaluated_at": "2026-08-14T00:00:00Z"},
        {"outcome_record_id": "o2", "decision_id": "d1", "status": "FINAL",
         "evaluated_at": "2026-08-14T00:01:00Z",
         "coverage": {"fraction": 0.9, "max_gap_seconds": 4.0},
         "net_return_fraction_by_slippage_ticks_per_side": {
             "0": 0.1, "1": 0.08, "2": 0.06}},
        {"outcome_record_id": "orphan", "decision_id": "missing",
         "status": "FINAL", "evaluated_at": "2026-08-13T17:00:00Z"},
    ]
    report = audit.build_report(
        audit_day="2026-08-13", decisions=decisions, episodes=episodes,
        no_trade=[], outcomes=outcomes, hypo_outcomes=[], bid_ticks=[],
        hypo_bid_ticks=[], collector={"collector_version": "v-test"},
        quarantine=[{"record_type": "QUARANTINE_ENTRY", "episode_record_id": "e1"}],
        a2_summary={
            "endpoint_source": "alpaca_historical_stock_quote_v1",
            "accrual_store": "dense_a2_v0",
            "accrual_summary_path": "/live/v2_data/a2_measurement/a2_summary_dense_v0.json",
            "clean_a2_eligible_episode_count": 1,
            "clean_a2_labelable_episode_count": 1,
            "clean_a2_unavailable_episode_count": 0,
            "missing_reason_counts": {},
            "n_collision_episode_rows_excluded": 0,
            "n_quarantined_episode_rows_excluded": 0,
            "data_integrity_status": "PASS",
            "phase4_preflight": "UNDERPOWERED_INCONCLUSIVE",
            "power_gate_reached": False,
        })

    ledger = report["entry_intelligence_ledger"]
    contract_data_v2 = report["option_bid_trackability_contract_data_v2"]
    assert ledger["episode_evaluations"] == 2
    assert ledger["admitted_episodes"] == 1
    assert ledger["rejected_episode_evaluations"] == 1
    assert ledger["episode_session_dates"] == {"MISSING": 2}
    assert ledger["session_date_mismatches"] == 2
    assert ledger["quarantine_sweep"] == {
        "admitted_quarantined": 1,
        "admitted_unquarantined": 0,
        "unquarantined_collision_groups": 0,
    }
    assert contract_data_v2["completed_outcomes"] == 1
    assert contract_data_v2["latest_outcome_statuses"] == {"FINAL": 1}
    assert contract_data_v2["outcome_linkage"] == {
        "unmatched_outcome_rows": 1,
        "unmatched_hypothetical_outcome_rows": 0,
        "date_basis": "parent_decision_decided_at",
    }
    assert contract_data_v2["net_return_outcomes"] == {
        "0_ticks": 1, "1_tick": 1, "2_ticks": 1}
    assert contract_data_v2["executable_bid_coverage"] == {
        "coverage": 0.9, "max_gap_seconds": 4.0}
    assert report["a2_accrual_mvp_edge"] == {
        "basis": "underlying_forward_return_a2_mvp_edge",
        "status": "MEASURED",
        "endpoint_source": "alpaca_historical_stock_quote_v1",
        "accrual_store": "dense_a2_v0",
        "accrual_summary_path": "/live/v2_data/a2_measurement/a2_summary_dense_v0.json",
        "clean_a2_eligible_episode_count": 1,
        "clean_a2_labelable_episode_count": 1,
        "clean_a2_unavailable_episode_count": 0,
        "a2_unavailable_reason_counts": {},
        "collision_episode_rows_excluded": 0,
        "quarantined_episode_rows_excluded": 0,
        "accrual_target_clean_a2_episodes": 200,
        "remaining_to_accrual_target": 199,
        "data_integrity_status": "PASS",
        "phase4_preflight": "UNDERPOWERED_INCONCLUSIVE",
        "power_gate_reached": False,
    }


def test_evaluated_at_does_not_reassign_outcome_to_a_later_day():
    decisions = [{"decision_id": "d1", "decided_at": "2026-08-13T23:59:00Z"}]
    outcomes = [{"decision_id": "d1", "status": "FINAL",
                 "evaluated_at": "2026-08-14T00:01:00Z"}]
    report = audit.build_report(
        audit_day="2026-08-14", decisions=decisions, episodes=[], no_trade=[],
        outcomes=outcomes, hypo_outcomes=[], bid_ticks=[], hypo_bid_ticks=[],
        collector={})
    assert report["option_bid_trackability_contract_data_v2"]["completed_outcomes"] == 0
    assert report["option_bid_trackability_contract_data_v2"]["latest_outcome_statuses"] == {}


def test_legacy_a2_summary_cannot_drive_authoritative_accrual():
    scoreboard = audit.a2_accrual_scoreboard({
        "endpoint_source": "live_tick_log",
        "clean_a2_labelable_episode_count": 99,
    })

    assert scoreboard["status"] == "A2_MEASUREMENT_ERROR"
    assert scoreboard["clean_a2_labelable_episode_count"] is None
    assert scoreboard["error"] == "authoritative_dense_a2_provenance_required"
