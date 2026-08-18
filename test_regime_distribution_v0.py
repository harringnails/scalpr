import json

import regime_distribution_v0 as distribution


def _episode(index, state, *, config_version="entry-intelligence-config-v1.2.0"):
    return {
        "episode_record_id": f"record-{index}",
        "episode_key": f"episode-{index}",
        "decision_id": f"decision-{index}",
        "config_version": config_version,
        "admitted": True,
        "regime_tag": {"state": state},
        # Poison-looking outcome data demonstrates it is irrelevant to this report.
        "signed_return_60m": "MUST_NOT_BE_READ",
    }


def _episodes(counts):
    rows = []
    for state, count in counts.items():
        for _ in range(count):
            rows.append(_episode(len(rows), state))
    return rows


def test_viable_at_pre_registered_boundaries():
    report = distribution.build_viability_report(_episodes({
        "TREND_UP": 70,
        "RANGE": 30,
    }))

    assert report["verdict"] == "VIABLE"
    assert report["criteria"]["largest_observed_state_fraction_of_classified"] == 0.7
    assert report["criteria"]["observed_states_at_minimum_cell_size"] == 2
    assert report["unknown"]["count"] == 0
    assert report["edge_computation_performed"] is False
    assert report["outcome_fields_read"] == []


def test_low_contrast_when_one_state_exceeds_seventy_percent():
    report = distribution.build_viability_report(_episodes({
        "TREND_UP": 71,
        "RANGE": 29,
    }))

    assert report["verdict"] == "LOW_CONTRAST"
    assert report["criteria"]["concentration_pass"] is False
    assert report["criteria"]["cell_size_pass"] is False


def test_low_contrast_when_fewer_than_two_cells_reach_thirty():
    report = distribution.build_viability_report(_episodes({
        "TREND_UP": 29,
        "TREND_DOWN": 29,
        "RANGE": 29,
    }))

    assert report["criteria"]["concentration_pass"] is True
    assert report["criteria"]["cell_size_pass"] is False
    assert report["verdict"] == "LOW_CONTRAST"


def test_unknown_at_twenty_percent_is_insufficient_tagging_and_separate():
    report = distribution.build_viability_report(_episodes({
        "TREND_UP": 40,
        "RANGE": 40,
        "UNKNOWN": 20,
    }))

    assert report["unknown"] == {
        "count": 20,
        "fraction_of_all_clean_episodes": 0.2,
    }
    assert report["n_classified_episodes"] == 80
    assert report["criteria"]["unknown_fraction_pass"] is False
    assert report["verdict"] == "INSUFFICIENT_TAGGING"


def test_criteria_use_exact_ratios_not_rounded_display_values():
    report = distribution.build_viability_report(_episodes({
        "TREND_UP": 58,
        "RANGE": 25,
        "UNKNOWN": 20,
    }))

    assert report["unknown"]["fraction_of_all_clean_episodes"] == 0.194175
    assert report["criteria"]["unknown_fraction_pass"] is True
    assert report["criteria"]["largest_observed_state_fraction_of_classified"] == 0.698795
    assert report["criteria"]["concentration_pass"] is True


def test_no_clean_episodes_is_insufficient_tagging():
    report = distribution.build_viability_report([])

    assert report["verdict"] == "INSUFFICIENT_TAGGING"
    assert report["unknown"]["fraction_of_all_clean_episodes"] is None


def test_loader_keeps_only_admitted_non_quarantined_v1_2(tmp_path):
    episodes_path = tmp_path / "episodes.jsonl"
    quarantine_path = tmp_path / "quarantine.jsonl"
    rows = [
        _episode(1, "TREND_UP"),
        _episode(2, "RANGE"),
        _episode(3, "HIGH_VOL", config_version="entry-intelligence-config-v1.1.0"),
        {**_episode(4, "TREND_DOWN"), "admitted": False},
    ]
    episodes_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    quarantine_path.write_text(json.dumps({
        "record_type": "QUARANTINE_ENTRY",
        "episode_record_id": "record-2",
    }) + "\n")

    loaded = distribution.load_clean_v1_2_episodes(episodes_path, quarantine_path)

    assert [row["episode_record_id"] for row in loaded] == ["record-1"]
