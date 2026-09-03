#!/usr/bin/env python3
"""Forward-only, read-only market-context capture for an isolated shadow ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import time
from datetime import datetime, time as clock_time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "market-context-shadow-v0"
STUDY_STATUS = "EXPLORATORY - NON-INFERENTIAL"
DEFAULT_OUTPUT = Path("market_context_shadow_v0.jsonl")
ETF_SYMBOLS = ("SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC")
LARGECAP_PROXY_SYMBOLS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK.B", "AVGO", "TSLA", "JPM")
ALL_SYMBOLS = tuple(dict.fromkeys(ETF_SYMBOLS + LARGECAP_PROXY_SYMBOLS))
ET = ZoneInfo("America/New_York")


def _parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _freshness(age: float | None, clean_seconds: float, stale_seconds: float) -> str:
    if age is None or age < -1.0 or age > stale_seconds:
        return "STALE"
    if age <= clean_seconds:
        return "CLEAN"
    return "DEGRADED"


def field(
    value: Any,
    *,
    source: str,
    provider_ts: Any,
    observed_at: datetime,
    clean_seconds: float = 15.0,
    stale_seconds: float = 60.0,
    classification: str = "direct",
) -> dict[str, Any]:
    provider = _parse_ts(provider_ts)
    age = (observed_at - provider).total_seconds() if provider else None
    state = _freshness(age, clean_seconds, stale_seconds)
    status = "AVAILABLE" if value is not None else "MISSING"
    if age is not None and age < -1.0:
        value = None
        status = "FUTURE_REJECTED"
    if value is None:
        state = "STALE"
    return {
        "age_seconds": round(age, 6) if age is not None else None,
        "classification": classification,
        "data_freshness": state,
        "provider_timestamp_utc": _iso(provider) if provider else None,
        "source": source,
        "status": status,
        "value": value,
    }


def _mid(quote: dict[str, Any] | None) -> float | None:
    if not quote:
        return None
    bid, ask = quote.get("bid"), quote.get("ask")
    if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (float(bid) + float(ask)) / 2.0


def _bar_ts(bar: dict[str, Any]) -> Any:
    return bar.get("timestamp") or bar.get("ts")


def _vwap(bars: list[dict[str, Any]]) -> float | None:
    usable = [bar for bar in bars if (bar.get("volume") or 0) > 0 and isinstance(bar.get("close"), (int, float))]
    volume = sum(float(bar["volume"]) for bar in usable)
    if not volume:
        return None
    return sum(float(bar["close"]) * float(bar["volume"]) for bar in usable) / volume


def _session_return(bars: list[dict[str, Any]], current: float | None) -> float | None:
    if not bars or current is None:
        return None
    opening = bars[0].get("open")
    if not isinstance(opening, (int, float)) or opening <= 0:
        return None
    return ((current / float(opening)) - 1.0) * 100.0


def _realized_vol(bars: list[dict[str, Any]]) -> float | None:
    closes = [float(bar["close"]) for bar in bars if isinstance(bar.get("close"), (int, float)) and bar["close"] > 0]
    if len(closes) < 3:
        return None
    returns = [math.log(right / left) for left, right in zip(closes, closes[1:])]
    return statistics.stdev(returns) * math.sqrt(390.0) * 100.0


def latest_flashalpha(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    latest: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("record_type") == "PIN_CANDIDATE" and row.get("symbol") == "SPY":
                latest = row
    return latest


def _flashalpha_fields(candidate: dict[str, Any] | None, observed_at: datetime) -> dict[str, Any]:
    evidence = (candidate or {}).get("evidence", {})
    freshness = evidence.get("data_freshness", {})
    timestamps = freshness.get("as_of_by_endpoint", {})
    provider_ts = timestamps.get("zero_dte") or (candidate or {}).get("observed_at_utc")
    call_wall = evidence.get("call_wall")
    put_wall = evidence.get("put_wall")
    values = {
        "call_wall": call_wall.get("price") if isinstance(call_wall, dict) else call_wall,
        "gamma_flip": evidence.get("gamma_flip"),
        "gamma_regime": evidence.get("gamma_regime"),
        "put_wall": put_wall.get("price") if isinstance(put_wall, dict) else put_wall,
        "spot": evidence.get("spot"),
        "spot_to_call_wall_bps": evidence.get("spot_to_call_wall_bps"),
        "spot_to_put_wall_bps": evidence.get("spot_to_put_wall_bps"),
        "zero_dte_gex_share_pct": evidence.get("zero_dte_gex_share_pct"),
    }
    return {
        name: field(
            value, source="FlashAlpha:gex+levels+zero_dte", provider_ts=provider_ts,
            observed_at=observed_at, clean_seconds=300.0, stale_seconds=600.0,
        )
        for name, value in values.items()
    }


def build_record(raw: dict[str, Any], *, observed_at: datetime, flashalpha: dict[str, Any] | None = None) -> dict[str, Any]:
    quotes = raw.get("quotes", {})
    bars = {
        symbol: [
            bar for bar in symbol_bars
            if _parse_ts(_bar_ts(bar)) is not None and _parse_ts(_bar_ts(bar)) <= observed_at
        ]
        for symbol, symbol_bars in raw.get("bars", {}).items()
    }
    spy_quote = quotes.get("SPY")
    spy_mid = _mid(spy_quote)
    spy_bars = bars.get("SPY", [])
    bar_provider_ts = _bar_ts(spy_bars[-1]) if spy_bars else None
    quote_provider_ts = (spy_quote or {}).get("timestamp")
    vwap_now = _vwap(spy_bars)
    prior_vwap = _vwap(spy_bars[:-5]) if len(spy_bars) > 5 else None
    opening = spy_bars[:30]
    opening_high = max((bar.get("high") for bar in opening if isinstance(bar.get("high"), (int, float))), default=None)
    opening_low = min((bar.get("low") for bar in opening if isinstance(bar.get("low"), (int, float))), default=None)
    if spy_mid is None or opening_high is None or opening_low is None:
        opening_state = None
    elif spy_mid > opening_high:
        opening_state = "ABOVE"
    elif spy_mid < opening_low:
        opening_state = "BELOW"
    else:
        opening_state = "INSIDE"
    spread_bps = None
    if spy_mid and spy_quote:
        spread_bps = (float(spy_quote["ask"]) - float(spy_quote["bid"])) / spy_mid * 10000.0
    structure_values = {
        "opening_range_30m_state": (opening_state, bar_provider_ts),
        "realized_vol_proxy_pct": (_realized_vol(spy_bars), bar_provider_ts),
        "spread_bps": (round(spread_bps, 6) if spread_bps is not None else None, quote_provider_ts),
        "spy_mid": (round(spy_mid, 6) if spy_mid is not None else None, quote_provider_ts),
        "vwap_distance_bps": (
            round((spy_mid - vwap_now) / vwap_now * 10000.0, 6)
            if spy_mid is not None and vwap_now else None,
            bar_provider_ts,
        ),
        "vwap_proxy": (round(vwap_now, 6) if vwap_now is not None else None, bar_provider_ts),
        "vwap_slope_proxy": (
            round(vwap_now - prior_vwap, 6) if vwap_now is not None and prior_vwap is not None else None,
            bar_provider_ts,
        ),
    }
    structure = {
        name: field(value, source="Alpaca:SIP", provider_ts=ts, observed_at=observed_at,
                    clean_seconds=90.0, stale_seconds=180.0,
                    classification="proxy" if "proxy" in name else "direct")
        for name, (value, ts) in structure_values.items()
    }

    cross_asset: dict[str, Any] = {}
    for symbol in ETF_SYMBOLS[1:]:
        current = _mid(quotes.get(symbol))
        symbol_bars = bars.get(symbol, [])
        ts = _bar_ts(symbol_bars[-1]) if symbol_bars else None
        cross_asset[f"{symbol.lower().replace('.', '_')}_session_return_pct"] = field(
            _session_return(symbol_bars, current), source="Alpaca:SIP", provider_ts=ts,
            observed_at=observed_at, clean_seconds=90.0, stale_seconds=180.0,
        )

    advances: list[bool] = []
    breadth_timestamps: list[datetime] = []
    for symbol in LARGECAP_PROXY_SYMBOLS:
        symbol_bars = bars.get(symbol, [])
        current = _mid(quotes.get(symbol))
        session_return = _session_return(symbol_bars, current)
        if session_return is not None:
            advances.append(session_return > 0)
        if symbol_bars and _parse_ts(_bar_ts(symbol_bars[-1])):
            breadth_timestamps.append(_parse_ts(_bar_ts(symbol_bars[-1])))
    breadth_value = sum(advances) / len(advances) if advances else None
    breadth_ts = min(breadth_timestamps) if breadth_timestamps else None
    breadth = {
        "largecap_breadth_proxy": field(
            round(breadth_value, 6) if breadth_value is not None else None,
            source="Alpaca:SIP:fixed_10_name_subset", provider_ts=breadth_ts,
            observed_at=observed_at, clean_seconds=90.0, stale_seconds=180.0,
            classification="proxy",
        ),
        "proxy_constituent_count": field(
            len(advances), source="Alpaca:SIP:fixed_10_name_subset", provider_ts=breadth_ts,
            observed_at=observed_at, clean_seconds=90.0, stale_seconds=180.0,
            classification="proxy",
        ),
        "proxy_symbols": field(
            list(LARGECAP_PROXY_SYMBOLS), source="configuration:fixed_10_name_subset",
            provider_ts=observed_at, observed_at=observed_at, classification="proxy",
        ),
    }
    groups = {
        "cross_asset": cross_asset,
        "largecap_breadth": {"largecap_breadth_proxy": breadth["largecap_breadth_proxy"]},
        "options_structure": _flashalpha_fields(flashalpha, observed_at),
        "spy_structure": structure,
    }
    freshness_states = [
        envelope["data_freshness"]
        for group in groups.values()
        for envelope in group.values()
        if isinstance(envelope, dict) and "data_freshness" in envelope
    ]
    overall = "STALE" if "STALE" in freshness_states else "DEGRADED" if "DEGRADED" in freshness_states else "CLEAN"
    record = {
        "admission_authority": False,
        "capture_state": "OBSERVATION_ONLY_NOT_SCORED",
        "data_freshness": overall,
        "execution_authority": False,
        "fields": {**groups, "largecap_breadth": breadth},
        "guard_access": False,
        "is_inferential": False,
        "observed_at_utc": _iso(observed_at),
        "record_type": "MARKET_CONTEXT_OBSERVATION",
        "schema_version": SCHEMA_VERSION,
        "study_status": STUDY_STATUS,
        "sub_scores": "DEFERRED_UNTIL_OPERATOR_LOCK",
    }
    record["record_hash"] = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
    return record


class AlpacaSipSource:
    """Read-only stock quote/bar adapter; it exposes no trading client."""

    def __init__(self) -> None:
        key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("source load_keychain_env.sh before running context capture")
        from alpaca.data.historical import StockHistoricalDataClient
        self.client = StockHistoricalDataClient(key, secret)

    def fetch(self, now: datetime) -> dict[str, Any]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
        from alpaca.data.timeframe import TimeFrame
        et_now = now.astimezone(ET)
        session_start = datetime.combine(et_now.date(), clock_time(9, 30), ET)
        quote_response = self.client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=list(ALL_SYMBOLS), feed=DataFeed.SIP)
        )
        bar_response = self.client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=list(ALL_SYMBOLS), timeframe=TimeFrame.Minute,
            start=session_start, end=now, feed=DataFeed.SIP,
        ))
        quotes: dict[str, Any] = {}
        bars: dict[str, list[dict[str, Any]]] = {}
        for symbol in ALL_SYMBOLS:
            quote = quote_response.get(symbol)
            quotes[symbol] = {
                "ask": float(quote.ask_price) if quote and quote.ask_price else None,
                "bid": float(quote.bid_price) if quote and quote.bid_price else None,
                "timestamp": _iso(quote.timestamp) if quote and quote.timestamp else None,
            }
            bars[symbol] = [
                {
                    "close": float(bar.close), "high": float(bar.high),
                    "low": float(bar.low), "open": float(bar.open),
                    "timestamp": _iso(bar.timestamp), "volume": float(bar.volume),
                }
                for bar in (bar_response.data.get(symbol) or [])
                if bar.timestamp <= now
            ]
        return {"bars": bars, "quotes": quotes}


def in_rth(now: datetime) -> bool:
    local = now.astimezone(ET)
    return local.weekday() < 5 and clock_time(9, 30) <= local.time() < clock_time(16, 0)


def append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_canonical(record) + "\n")


def capture_once(source: Any, *, output: Path, flashalpha_ledger: Path | None, now: datetime) -> dict[str, Any]:
    raw = source.fetch(now)
    record = build_record(raw, observed_at=now, flashalpha=latest_flashalpha(flashalpha_ledger))
    append_record(output, record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="capture forward-only RTH observations")
    capture.add_argument("--polls", type=int, default=1)
    capture.add_argument("--interval-seconds", type=float, default=60.0)
    capture.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    capture.add_argument("--flashalpha-ledger", type=Path)
    args = parser.parse_args()
    if not 30.0 <= args.interval_seconds <= 60.0:
        raise SystemExit("--interval-seconds must be between 30 and 60")
    if args.polls < 1:
        raise SystemExit("--polls must be positive")
    source = AlpacaSipSource()
    written = 0
    for index in range(args.polls):
        now = datetime.now(timezone.utc)
        if not in_rth(now):
            raise SystemExit("context capture is RTH-only (09:30-16:00 America/New_York)")
        record = capture_once(
            source, output=args.output, flashalpha_ledger=args.flashalpha_ledger, now=now,
        )
        written += 1
        print(_canonical({
            "data_freshness": record["data_freshness"],
            "execution_authority": False,
            "observed_at_utc": record["observed_at_utc"],
            "record_hash": record["record_hash"],
        }), flush=True)
        if index + 1 < args.polls:
            time.sleep(args.interval_seconds)
    print(_canonical({"output": str(args.output), "records_written": written, "study_status": STUDY_STATUS}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
