from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import option_scenario_v0 as scenario


NOW = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("symbol", "expected_price", "expected_pct"),
    [
        ("SPY260908C00100000", 102.1, 2.1),
        ("SPY260908P00100000", 97.9, -2.1),
    ],
)
def test_call_and_put_breakeven_use_live_ask(symbol, expected_price, expected_pct):
    result = scenario.calculate_scenario(
        option_symbol=symbol, quantity=1, spot=100, bid=1.9, ask=2.1, observed_at=NOW,
    )
    assert result["breakeven"] == {
        "premium_basis": "live_executable_ask",
        "premium_points": 2.1,
        "price": expected_price,
        "underlying_move_pct": expected_pct,
    }


def test_iv_inversion_round_trips_known_black_scholes_price():
    years = 30 / 365
    modeled = scenario.bs_price_greeks(
        spot=100, strike=100, years=years, iv=0.2, right="C",
    )
    inverted = scenario.implied_volatility(
        option_mid=modeled["price"], spot=100, strike=100, years=years, right="C",
    )
    assert inverted == pytest.approx(0.2, abs=1e-8)


def test_delta_gamma_scenario_uses_frozen_second_order_formula():
    result = scenario.calculate_scenario(
        option_symbol="SPY260908C00100000", quantity=1, spot=100, bid=1.9, ask=2.1,
        observed_at=NOW,
        flash_contract={"status": "FRESH", "iv": 0.2, "delta": 0.5, "gamma": 0.1},
    )
    row = next(row for row in result["scenario_rows"] if row["underlying_move_points"] == 1.0)
    assert row["option_change_points"] == 0.55
    assert row["pnl_pct"] == 27.5
    assert row["pnl_dollars"] == 55.0


def test_missing_iv_and_greeks_preserves_breakeven_and_marks_rows_unavailable():
    result = scenario.calculate_scenario(
        option_symbol="SPY260908C00090000", quantity=2, spot=100, bid=9.9, ask=10.1,
        observed_at=NOW,
    )
    assert result["breakeven"]["price"] == 100.1
    assert result["expected_move"] is None
    assert result["greeks"]["source"] is None
    assert all(row["status"] == "UNAVAILABLE" for row in result["scenario_rows"])


def test_flashalpha_contract_requires_fresh_provider_timestamp():
    payload = {
        "as_of": "2026-09-08T14:59:00Z",
        "data": [{"strike": 100, "call_iv": 0.21, "call_delta": 0.52, "call_gamma": 0.08}],
    }
    fresh = scenario.extract_flash_contract(payload, strike=100, right="C", observed_at=NOW)
    assert fresh["status"] == "FRESH"
    assert fresh["iv"] == 0.21

    payload["as_of"] = "2026-09-08T14:50:00Z"
    stale = scenario.extract_flash_contract(payload, strike=100, right="C", observed_at=NOW)
    assert stale["status"] == "STALE_OR_MISSING"


def test_time_to_expiry_uses_sixteen_hundred_new_york():
    years = scenario.time_to_expiry_years(date(2026, 9, 8), NOW)
    assert years == pytest.approx(5 / 24 / 365)


def test_flashalpha_failure_preserves_quote_breakeven_and_bs_fallback(monkeypatch):
    class OptionClient:
        def get_option_latest_quote(self, request):
            return {"SPY260908C00100000": SimpleNamespace(
                bid_price=1.9, ask_price=2.1, timestamp=NOW,
            )}

    class StockClient:
        def get_stock_latest_quote(self, request):
            return {"SPY": SimpleNamespace(
                bid_price=99.99, ask_price=100.01, timestamp=NOW,
            )}

    class FailingHttp:
        @staticmethod
        def get(*args, **kwargs):
            raise scenario.requests.Timeout("fixture")

    source = scenario.LiveScenarioSource.__new__(scenario.LiveScenarioSource)
    source.option_client = OptionClient()
    source.stock_client = StockClient()
    source.http = FailingHttp()
    monkeypatch.setattr(scenario, "load_keychain_secret", lambda **kwargs: "fixture-key")

    result = source.fetch("SPY260908C00100000", 1, NOW)
    assert result["status"] == "AVAILABLE"
    assert result["breakeven"]["price"] == 102.1
    assert result["expected_move"]["iv_source"] == "black_scholes_inversion:live_opra_mid"
    assert result["provenance"]["flashalpha"]["status"] == "UNAVAILABLE"
