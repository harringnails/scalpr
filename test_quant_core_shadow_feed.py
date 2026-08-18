from __future__ import annotations

import ast
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import quant_core_shadow_feed as feed


def _quote(
    contract_id: str,
    *,
    expiration: date,
    bid: str,
    ask: str,
    resolution: str,
    age_ms: int,
):
    observed = datetime(2026, 8, 14, 13, 59, tzinfo=UTC) - timedelta(
        milliseconds=age_ms
    )
    return SimpleNamespace(
        contract_id=contract_id,
        underlying="SPY",
        expiration=expiration,
        strike=Decimal(650),
        option_type=SimpleNamespace(value="call"),
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=12,
        source_timestamp=observed,
        ingest_timestamp=observed + timedelta(milliseconds=1),
        quote_age_ms=age_ms,
        resolution=SimpleNamespace(value=resolution),
    )


def _view(*quotes):
    as_of = datetime(2026, 8, 14, 13, 59, tzinfo=UTC)
    return SimpleNamespace(
        consumer=SimpleNamespace(value="scalpr"),
        order_routing_permitted=False,
        as_of_timestamp=as_of,
        dataset="OPRA.PILLAR",
        snapshot_id="snapshot-1",
        quotes=quotes,
        open_interest=(
            SimpleNamespace(
                contract_id=quotes[0].contract_id,
                oi=1234,
                source_business_date=date(2026, 8, 14),
            ),
        ),
    )


def test_shadow_feed_is_default_off() -> None:
    with pytest.raises(feed.ShadowFeedDisabled, match="default-off"):
        feed._require_enabled({})
    feed._require_enabled({feed.ENABLE_ENV: "1"})


def test_historical_minute_quotes_are_research_only_not_live_executable() -> None:
    as_of_date = date(2026, 8, 14)
    view = _view(
        _quote(
            "SPY   260814C00650000",
            expiration=as_of_date,
            bid="2.00",
            ask="2.10",
            resolution="1m",
            age_ms=0,
        ),
        _quote(
            "SPY   260815C00650000",
            expiration=as_of_date + timedelta(days=1),
            bid="1.00",
            ask="2.00",
            resolution="1m",
            age_ms=0,
        ),
        _quote(
            "SPY   260818C00650000",
            expiration=as_of_date + timedelta(days=4),
            bid="2.00",
            ask="2.05",
            resolution="1m",
            age_ms=0,
        ),
    )
    record = feed.build_shadow_record(
        view,
        mode="historical_replay",
        captured_at=datetime(2026, 8, 16, tzinfo=UTC),
    )

    assert record["status"] == "RESEARCH_READY"
    assert record["counts"] == {
        "automated_scope_quotes": 2,
        "two_sided_quotes": 2,
        "approved_spread_quotes": 1,
        "live_gate_pass_quotes": 0,
        "open_interest_covered_quotes": 1,
    }
    assert record["execution_authority"] is False
    assert record["order_routing_permitted"] is False
    assert record["replaces_alpaca"] is False


def test_fresh_tick_must_pass_spread_gate_to_be_live_observer_ready() -> None:
    expiration = date(2026, 8, 14)
    record = feed.build_shadow_record(
        _view(
            _quote(
                "SPY   260814C00650000",
                expiration=expiration,
                bid="2.00",
                ask="2.10",
                resolution="tick",
                age_ms=2_000,
            )
        ),
        mode="live_observer",
        captured_at=datetime(2026, 8, 14, 13, 59, tzinfo=UTC),
    )
    assert record["status"] == "OBSERVER_READY"
    assert record["counts"]["live_gate_pass_quotes"] == 1


def test_append_is_idempotent_and_hash_chain_is_validated(tmp_path) -> None:
    view = _view(
        _quote(
            "SPY   260814C00650000",
            expiration=date(2026, 8, 14),
            bid="2.00",
            ask="2.10",
            resolution="1m",
            age_ms=0,
        )
    )
    record = feed.build_shadow_record(
        view,
        mode="historical_replay",
        captured_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    target = tmp_path / "shadow.jsonl"

    assert feed.append_shadow_record(record, target) is True
    assert feed.append_shadow_record(record, target) is False
    assert feed._read_evidence(target)[0]["record_hash"] == record["record_hash"]


def test_reconciliation_is_descriptive_and_never_promotes() -> None:
    quant_time = datetime(2026, 8, 14, 13, 59, tzinfo=UTC)
    shadow = feed.build_shadow_record(
        _view(
            _quote(
                "SPY   260814C00650000",
                expiration=date(2026, 8, 14),
                bid="2.00",
                ask="2.10",
                resolution="tick",
                age_ms=0,
            )
        ),
        mode="live_observer",
        captured_at=quant_time,
    )
    alpaca = [
        {
            "source": "alpaca_options_latest_quote:opra",
            "status": "FRESH",
            "option_symbol": "SPY   260814C00650000",
            "observed_at": (quant_time + timedelta(seconds=2)).isoformat(),
            "bid": 1.99,
            "ask": 2.11,
            "quote_id": "alpaca-quote-1",
        }
    ]

    record = feed.build_reconciliation_record(
        shadow,
        alpaca,
        captured_at=quant_time + timedelta(minutes=1),
        tolerance_seconds=5.0,
    )

    assert record["status"] == "MEASURED"
    assert record["counts"]["matched_contracts"] == 1
    assert record["metrics"]["median_absolute_mid_difference"] == 0.0
    assert record["promotion_eligible"] is False
    assert record["execution_authority"] is False
    assert record["order_routing_permitted"] is False


def test_module_has_no_broker_guard_or_order_imports() -> None:
    tree = ast.parse(Path(feed.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    forbidden = ("scalp_server", "wave_order_adapter", "alpaca.trading")
    assert all(
        not any(name == blocked or name.startswith(f"{blocked}.") for blocked in forbidden)
        for name in imported
    )
