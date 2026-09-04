import inspect
from pathlib import Path

import option_scenario_readonly_server_v0 as bridge
import option_scenario_v0 as scenario


ROOT = Path(__file__).resolve().parent


def test_bridge_exposes_get_only_and_has_no_trade_authority():
    source = (ROOT / "option_scenario_readonly_server_v0.py").read_text()
    assert 'parsed.path == "/health"' in source
    assert 'parsed.path != "/scenario"' in source
    assert "do_POST" in source and "HTTPStatus.METHOD_NOT_ALLOWED" in source
    assert "do_PUT = do_POST" in source
    assert "do_PATCH = do_POST" in source
    assert "do_DELETE = do_POST" in source
    assert '"execution_authority": False' in source
    assert "TradingClient" not in source


def test_live_source_constructs_only_market_data_clients():
    source = inspect.getsource(scenario.LiveScenarioSource)
    assert "OptionHistoricalDataClient" in source
    assert "StockHistoricalDataClient" in source
    assert "TradingClient" not in source
    assert "submit" not in source.lower()
    assert "order" not in source.lower()


def test_unavailable_response_is_still_labeled_non_authoritative():
    result = bridge.unavailable("fixture")
    assert result["execution_authority"] is False
    assert result["admission_authority"] is False
    assert result["is_forecast"] is False
    assert result["status"] == "UNAVAILABLE"
