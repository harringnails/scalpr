from copy import deepcopy

import market_context_index_v0 as index


def field(value, freshness="CLEAN", status="AVAILABLE"):
    return {"value": value, "data_freshness": freshness, "status": status}


def record():
    cross = {
        "qqq_session_return_pct": field(0.4),
        "iwm_session_return_pct": field(0.2),
    }
    cross.update({key: field(0.1) for key in index.SECTOR_KEYS})
    return {
        "data_freshness": "CLEAN",
        "fields": {
            "spy_structure": {
                "vwap_distance_bps": field(8.0),
                "opening_range_30m_state": field("ABOVE"),
            },
            "largecap_breadth": {"largecap_breadth_proxy": field(0.7)},
            "cross_asset": cross,
            "options_structure": {"gamma_regime": field("positive")},
        },
    }


def test_context_index_is_deterministic_and_bounded():
    first = index.compute_context_index(record())
    second = index.compute_context_index(deepcopy(record()))
    assert first == second
    assert first["status"] == "SCORED"
    assert first["score"] == 88
    assert 0 <= first["score"] <= 100
    assert sum(first["weights"].values()) == 1.0


def test_stale_or_any_required_group_input_missing_is_not_scored():
    stale = record()
    stale["data_freshness"] = "STALE"
    assert index.compute_context_index(stale)["score"] is None
    missing = record()
    missing["fields"]["cross_asset"]["iwm_session_return_pct"] = field(None, status="MISSING")
    assert index.compute_context_index(missing)["reason"] == "GROUP_INPUT_MISSING"
    assert index.compute_context_index(None)["reason"] == "NO_RECORD"


def test_options_regime_cannot_move_directional_score_by_itself():
    neutral = record()
    neutral["fields"]["spy_structure"]["vwap_distance_bps"] = field(0)
    neutral["fields"]["spy_structure"]["opening_range_30m_state"] = field("INSIDE")
    neutral["fields"]["largecap_breadth"]["largecap_breadth_proxy"] = field(0.5)
    for envelope in neutral["fields"]["cross_asset"].values():
        envelope["value"] = 0
    positive = index.compute_context_index(neutral)
    neutral["fields"]["options_structure"]["gamma_regime"]["value"] = "negative"
    negative = index.compute_context_index(neutral)
    assert positive["score"] == negative["score"] == 50
    assert positive["group_leans"]["options_structure"] == 0
    assert positive["options_modifier"] != negative["options_modifier"]


def test_label_is_explicitly_non_predictive():
    result = index.compute_context_index(record())
    assert result["label"] == (
        "exploratory composite · not a probability · not a signal · "
        "present conditions, not a forecast."
    )
    assert result["is_inferential"] is False
