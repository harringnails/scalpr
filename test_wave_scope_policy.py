"""Network-free tests for the versioned Wave shadow scope."""

from __future__ import annotations

from datetime import date

import scope_policy
import wave_scope_policy as wsp


def _blocked(fn):
    try:
        fn()
    except scope_policy.ScopeError:
        return True
    return False


def test_standard_scope_stays_spy_only():
    assert _blocked(lambda: scope_policy.validate_option(
        "VOO260814C00700000", date(2026, 8, 4)))


def test_voo_call_shadow_scope_is_versioned():
    parsed = wsp.validate_contract_direction(
        "VOO260814C00700000", "CALL", date(2026, 8, 4))
    assert parsed["dte"] == 10
    assert parsed["research_cohort_id"] == wsp.VOO_CALL_COHORT_ID
    assert parsed["scope_version"] == wsp.SCOPE_VERSION


def test_voo_put_and_wrong_dte_fail_closed():
    assert _blocked(lambda: wsp.validate_option(
        "VOO260814P00700000", date(2026, 8, 4)))
    assert _blocked(lambda: wsp.validate_option(
        "VOO260805C00700000", date(2026, 8, 4)))
    assert _blocked(lambda: wsp.validate_contract_direction(
        "VOO260814C00700000", "PUT", date(2026, 8, 4)))


def test_qqq_calls_and_puts_use_separate_shadow_cohort():
    call = wsp.validate_contract_direction(
        "QQQ260814C00700000", "CALL", date(2026, 8, 4))
    put = wsp.validate_contract_direction(
        "QQQ260814P00700000", "PUT", date(2026, 8, 4))
    assert call["research_cohort_id"] == wsp.QQQ_COHORT_ID
    assert put["research_cohort_id"] == wsp.QQQ_COHORT_ID
    assert _blocked(lambda: wsp.validate_option(
        "QQQ260805C00700000", date(2026, 8, 4)))


def test_other_research_tickers_are_versioned_and_dte_bounded():
    call = wsp.validate_contract_direction(
        "IWM260814C00300000", "CALL", date(2026, 8, 4))
    put = wsp.validate_contract_direction(
        "AAPL260904P00200000", "PUT", date(2026, 8, 4))
    assert call["research_cohort_id"] == wsp.MULTI_UNDERLYING_COHORT_ID
    assert put["research_cohort_id"] == wsp.MULTI_UNDERLYING_COHORT_ID
    assert _blocked(lambda: wsp.validate_underlying("BAD-TICKER"))
    for index in ("NDX", "NDXP", "RUT", "VIX"):
        assert _blocked(lambda index=index: wsp.validate_underlying(index))
    assert _blocked(lambda: wsp.validate_option(
        "IWM260805C00300000", date(2026, 8, 4)))


def test_spx_and_spxw_use_native_index_cohort():
    assert wsp.validate_underlying("SPX") == "SPX"
    assert wsp.validate_underlying("SPXW") == "SPX"
    weekly = wsp.validate_contract_direction(
        "SPXW260918C07700000", "CALL", date(2026, 8, 5))
    standard = wsp.validate_contract_direction(
        "SPX260904P07000000", "PUT", date(2026, 8, 5))
    for parsed in (weekly, standard):
        assert parsed["underlying"] == "SPX"
        assert parsed["research_cohort_id"] == wsp.SPX_INDEX_COHORT_ID
        assert parsed["scope_version"] == wsp.SCOPE_VERSION
    assert weekly["contract_underlying"] == "SPXW"
    assert _blocked(lambda: wsp.validate_option(
        "SPXW261218C07700000", date(2026, 8, 5)))


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_"):
            test()
            print(f"PASS {name}")
