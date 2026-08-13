"""Network-free tests for the descriptive weighted precheck contract."""

from precheck import (
    WEIGHTED_SCORING_CONFIG_HASH,
    WEIGHTED_SCORING_VERSION,
    _sig,
    build_weighted_read,
)


def complete_signals(*, trend="bullish", momentum="bullish", fear="neutral",
                     breadth="neutral", leadership="neutral", rates="neutral",
                     credit="neutral"):
    return [
        _sig("Trend", trend, "trend"),
        _sig("Momentum", momentum, "momentum"),
        _sig("Volatility", "neutral", "volatility"),
        _sig("Fear gauge", fear, "fear"),
        _sig("Breadth", breadth, "breadth"),
        _sig("Tech leadership", leadership, "leadership"),
        _sig("Rates", rates, "rates"),
        _sig("Credit", credit, "credit"),
    ]


def test_tactical_yes_is_weighted_agreement_not_probability():
    read = build_weighted_read(
        complete_signals(fear="bearish"), [], "bullish")
    assert read["decision"] == "YES"
    assert read["agreement_pct"] == 85.7
    assert read["input_coverage_pct"] == 100.0
    assert read["is_probability"] is False
    assert read["is_recommendation"] is False
    assert read["version"] == WEIGHTED_SCORING_VERSION
    assert read["config_hash"] == WEIGHTED_SCORING_CONFIG_HASH


def test_tactical_no_is_relative_to_requested_position():
    read = build_weighted_read(
        complete_signals(fear="bullish"), [], "bearish")
    assert read["decision"] == "NO"
    assert read["agreement_pct"] == 100.0
    tactical = read["horizons"]["tactical"]
    assert tactical["support_weight"] == 0
    assert tactical["opposing_weight"] == 7


def test_mixed_or_incomplete_tactical_evidence_abstains():
    mixed = build_weighted_read(
        complete_signals(trend="bullish", momentum="bearish"), [], "bullish")
    assert mixed["decision"] == "NO READ"
    assert mixed["agreement_pct"] == 50.0
    assert any("neither tactical side" in reason for reason in mixed["reasons"])

    sparse = build_weighted_read([
        _sig("Trend", "bullish", "trend"),
        _sig("Breadth", "bullish", "breadth"),
    ], ["Momentum", "Volatility", "Fear gauge", "Tech leadership", "Rates", "Credit"],
        "bullish")
    assert sparse["decision"] == "NO READ"
    assert "Momentum" in sparse["missing_critical"]
    assert sparse["input_coverage_pct"] < 70


def test_coherent_group_is_collapsed_and_mixed_group_is_discounted():
    coherent = build_weighted_read(
        complete_signals(breadth="bullish", leadership="bullish"), [], "bullish")
    structural = coherent["horizons"]["structural"]
    assert structural["support_weight"] == 2
    assert structural["opposing_weight"] == 0
    assert structural["coherence"][0]["status"] == "coherent"

    mixed = build_weighted_read(
        complete_signals(breadth="bullish", leadership="bearish"), [], "bullish")
    structural = mixed["horizons"]["structural"]
    assert structural["support_weight"] == 1
    assert structural["opposing_weight"] == 1
    assert structural["decision"] == "NO READ"
    assert structural["coherence"][0]["status"] == "mixed"


def test_bias_notes_are_visible_but_do_not_claim_predictive_power():
    read = build_weighted_read(complete_signals(), [], "bullish")
    noted = {item["signal"] for item in read["bias_notes"]}
    assert noted == {"Fear gauge", "Rates", "Credit"}
    assert read["label"] == "weighted evidence agreement - not win probability"


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_"):
            test()
            print("PASS", name)
