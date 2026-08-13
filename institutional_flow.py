"""Provider-neutral institutional-flow contracts and deterministic features.

Version 0 is Unusual Whales-only, descriptive, and non-qualifying. It has no
broker imports and cannot create or approve a trade.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field


EVENT_SCHEMA_VERSION = "institutional-flow-event-v0"
SNAPSHOT_SCHEMA_VERSION = "institutional-flow-snapshot-v0"
CONFIG_VERSION = "institutional-flow-config-v0"
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


class InstitutionalFlowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["institutional-flow-event-v0"] = EVENT_SCHEMA_VERSION
    provider: Literal["unusual_whales"] = "unusual_whales"
    event_id: str
    dedupe_key: str
    observed_at: datetime
    received_at: datetime
    ticker: str
    option_symbol: Optional[str] = None
    expiration: Optional[datetime] = None
    strike: Optional[Decimal] = None
    option_type: Optional[Literal["CALL", "PUT"]] = None
    trade_price: Optional[Decimal] = None
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    trade_size: Optional[int] = None
    premium: Optional[Decimal] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    execution_side: Literal[
        "ASK", "BID", "MID", "ABOVE_ASK", "BELOW_BID", "UNKNOWN"
    ] = "UNKNOWN"
    sentiment: Literal["BULLISH", "BEARISH", "NEUTRAL", "UNKNOWN"] = "UNKNOWN"
    is_sweep: bool = False
    is_block: bool = False
    is_multi_leg: bool = False
    days_to_expiration: Optional[int] = None
    underlying_price: Optional[Decimal] = None
    source_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    provider_latency_ms: Optional[float] = Field(default=None, ge=0.0)
    raw_payload: dict[str, Any]


class InstitutionalFlowProvider(Protocol):
    async def fetch_recent_events(
        self, ticker: str, lookback_minutes: int
    ) -> list[InstitutionalFlowEvent]: ...

    async def stream_events(self, tickers: list[str]): ...

    async def health_check(self) -> bool: ...


class InstitutionalFlowSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["institutional-flow-snapshot-v0"] = SNAPSHOT_SCHEMA_VERSION
    ticker: str
    window_minutes: int
    window_start: datetime
    window_end: datetime
    institutional_flow_status: Literal["AVAILABLE", "STALE", "UNAVAILABLE"]
    institutional_flow_age_seconds: Optional[float] = None
    event_count: int
    net_call_premium: Optional[Decimal] = None
    net_put_premium: Optional[Decimal] = None
    net_directional_premium: Optional[Decimal] = None
    ask_side_call_premium: Optional[Decimal] = None
    ask_side_put_premium: Optional[Decimal] = None
    bid_side_call_premium: Optional[Decimal] = None
    bid_side_put_premium: Optional[Decimal] = None
    call_put_premium_ratio: Optional[float] = None
    ask_bid_premium_ratio: Optional[float] = None
    sweep_count: Optional[int] = None
    block_count: Optional[int] = None
    multi_leg_count: Optional[int] = None
    large_trade_count: Optional[int] = None
    unique_contract_count: Optional[int] = None
    unique_expiration_count: Optional[int] = None
    short_dated_premium: Optional[Decimal] = None
    same_day_expiration_premium: Optional[Decimal] = None
    premium_velocity: Optional[float] = None
    premium_acceleration: Optional[float] = None
    flow_event_velocity: Optional[float] = None
    repeated_strike_score: Optional[float] = None
    repeated_expiration_score: Optional[float] = None
    bullish_flow_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    bearish_flow_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    flow_direction_score: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    flow_concentration_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    flow_divergence_score: Optional[float] = None
    opening_position_probability: Optional[float] = None
    institutional_urgency_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    data_quality_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    provider_latency_ms: Optional[float] = None
    qualifying: bool = False
    recommendation: None = None


def _decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _integer(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) or str(value).isdigit():
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000.0
        parsed = datetime.fromtimestamp(raw, timezone.utc)
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _occ(symbol: Optional[str]):
    match = OCC.match(str(symbol or "").replace(" ", ""))
    if not match:
        return None, None, None
    _, ymd, right, strike = match.groups()
    expiration = datetime.strptime(ymd, "%y%m%d").replace(tzinfo=timezone.utc)
    return expiration, Decimal(strike) / Decimal(1000), "CALL" if right == "C" else "PUT"


def _fallback_key(parts: tuple[Any, ...]) -> str:
    body = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def normalize_unusual_whales_alert(
    row: dict[str, Any], *, received_at: datetime, provider_latency_ms: Optional[float] = None
) -> InstitutionalFlowEvent:
    """Normalize an official FlowAlert-like payload; reject missing identity/time."""
    ticker = str(row.get("ticker") or row.get("underlying_symbol") or "").strip().upper()
    option_symbol = row.get("option_chain") or row.get("option_symbol") or row.get("option_chain_id")
    if not ticker:
        raise ValueError("missing ticker")
    observed_raw = (row.get("executed_at") or row.get("end_time")
                    or row.get("start_time") or row.get("created_at"))
    if observed_raw is None:
        raise ValueError("missing observed timestamp")
    observed_at = _timestamp(observed_raw)
    received = _timestamp(received_at)
    if observed_at > received:
        raise ValueError("observed timestamp is after receipt timestamp")

    expiration, strike, option_type = _occ(option_symbol)
    if expiration is None and row.get("expiry"):
        expiration = _timestamp(str(row["expiry"])[:10])
    if strike is None:
        strike = _decimal(row.get("strike"))
    if option_type is None:
        raw_type = str(row.get("type") or row.get("option_type") or "").upper()
        option_type = "CALL" if raw_type in {"C", "CALL"} else "PUT" if raw_type in {"P", "PUT"} else None
    ask_premium = _decimal(row.get("total_ask_side_prem")) or Decimal(0)
    bid_premium = _decimal(row.get("total_bid_side_prem")) or Decimal(0)
    if ask_premium > bid_premium:
        side = "ASK"
    elif bid_premium > ask_premium:
        side = "BID"
    elif ask_premium or bid_premium:
        side = "MID"
    else:
        side = "UNKNOWN"
    if option_type == "CALL":
        sentiment = "BULLISH" if side == "ASK" else "BEARISH" if side == "BID" else "UNKNOWN"
    elif option_type == "PUT":
        sentiment = "BEARISH" if side == "ASK" else "BULLISH" if side == "BID" else "UNKNOWN"
    else:
        sentiment = "UNKNOWN"
    event_id = str(row.get("id") or row.get("event_id") or "").strip()
    dedupe_key = event_id or _fallback_key((
        "unusual_whales", option_symbol, observed_at.isoformat(), row.get("price"),
        row.get("total_size") or row.get("size"), row.get("total_premium"),
        tuple(row.get("exchanges") or []), bool(row.get("has_sweep")),
    ))
    dte = (expiration.date() - observed_at.date()).days if expiration else None
    return InstitutionalFlowEvent(
        event_id=event_id or dedupe_key,
        dedupe_key=dedupe_key,
        observed_at=observed_at,
        received_at=received,
        ticker=ticker,
        option_symbol=option_symbol,
        expiration=expiration,
        strike=strike,
        option_type=option_type,
        trade_price=_decimal(row.get("price")),
        bid=_decimal(row.get("bid") or row.get("nbbo_bid")),
        ask=_decimal(row.get("ask") or row.get("nbbo_ask")),
        trade_size=_integer(row.get("total_size") or row.get("size")),
        premium=_decimal(row.get("total_premium") or row.get("premium")),
        volume=_integer(row.get("volume")),
        open_interest=_integer(row.get("open_interest")),
        execution_side=side,
        sentiment=sentiment,
        is_sweep=bool(row.get("has_sweep") or row.get("is_sweep")),
        is_block=bool(row.get("has_block") or row.get("is_block")),
        is_multi_leg=bool(row.get("has_multileg") or row.get("is_multi_leg")),
        days_to_expiration=dte,
        underlying_price=_decimal(row.get("underlying_price")),
        source_confidence=None,
        provider_latency_ms=provider_latency_ms,
        raw_payload=dict(row),
    )


def aggregate_flow_events(
    events: list[InstitutionalFlowEvent], *, ticker: str, window_minutes: int,
    window_end: datetime, stale_after_seconds: int = 30,
    large_trade_threshold: Decimal = Decimal("100000"),
    provider_available: bool = False,
) -> InstitutionalFlowSnapshot:
    end = _timestamp(window_end)
    start = end.fromtimestamp(end.timestamp() - window_minutes * 60, timezone.utc)
    rows = sorted(
        [e for e in events if e.ticker == ticker.upper() and start <= e.observed_at <= end],
        key=lambda e: (e.observed_at, e.dedupe_key),
    )
    if not rows:
        if provider_available:
            return InstitutionalFlowSnapshot(
                ticker=ticker.upper(), window_minutes=window_minutes,
                window_start=start, window_end=end,
                institutional_flow_status="AVAILABLE",
                institutional_flow_age_seconds=0.0, event_count=0,
                net_call_premium=Decimal(0), net_put_premium=Decimal(0),
                net_directional_premium=Decimal(0),
                ask_side_call_premium=Decimal(0), ask_side_put_premium=Decimal(0),
                bid_side_call_premium=Decimal(0), bid_side_put_premium=Decimal(0),
                sweep_count=0, block_count=0, multi_leg_count=0,
                large_trade_count=0, unique_contract_count=0,
                unique_expiration_count=0, short_dated_premium=Decimal(0),
                same_day_expiration_premium=Decimal(0), premium_velocity=0.0,
                premium_acceleration=0.0, flow_event_velocity=0.0,
                repeated_strike_score=0.0, repeated_expiration_score=0.0,
                bullish_flow_score=0.0, bearish_flow_score=0.0,
                flow_direction_score=0.0, flow_concentration_score=0.0,
                institutional_urgency_score=0.0, data_quality_score=1.0,
            )
        return InstitutionalFlowSnapshot(
            ticker=ticker.upper(), window_minutes=window_minutes,
            window_start=start, window_end=end,
            institutional_flow_status="UNAVAILABLE",
            institutional_flow_age_seconds=None, event_count=0,
        )
    age = max(0.0, (end - max(e.observed_at for e in rows)).total_seconds())
    status = "STALE" if age > stale_after_seconds else "AVAILABLE"

    def premium(event):
        return event.premium or Decimal(0)

    def total(predicate):
        return sum((premium(e) for e in rows if predicate(e)), Decimal(0))

    def side_premium(event, side):
        provider_key = "total_ask_side_prem" if side == "ASK" else "total_bid_side_prem"
        supplied = _decimal(event.raw_payload.get(provider_key))
        if supplied is not None:
            return supplied
        return premium(event) if event.execution_side == side else Decimal(0)

    ask_call = sum((side_premium(e, "ASK") for e in rows if e.option_type == "CALL"), Decimal(0))
    bid_call = sum((side_premium(e, "BID") for e in rows if e.option_type == "CALL"), Decimal(0))
    ask_put = sum((side_premium(e, "ASK") for e in rows if e.option_type == "PUT"), Decimal(0))
    bid_put = sum((side_premium(e, "BID") for e in rows if e.option_type == "PUT"), Decimal(0))
    call_total, put_total = ask_call + bid_call, ask_put + bid_put
    ask_total, bid_total = ask_call + ask_put, bid_call + bid_put
    net_call, net_put = ask_call - bid_call, ask_put - bid_put
    directional = net_call - net_put
    bullish = ask_call + bid_put
    bearish = ask_put + bid_call
    classified = bullish + bearish
    total_premium = sum((premium(e) for e in rows), Decimal(0))
    half = start.fromtimestamp(start.timestamp() + window_minutes * 30, timezone.utc)
    early = sum((premium(e) for e in rows if e.observed_at < half), Decimal(0))
    late = total_premium - early
    half_minutes = max(window_minutes / 2.0, 0.5)
    strike_counts = {}
    expiry_counts = {}
    contract_premium = {}
    for event in rows:
        if event.strike is not None:
            strike_counts[event.strike] = strike_counts.get(event.strike, 0) + 1
        if event.expiration is not None:
            key = event.expiration.date().isoformat()
            expiry_counts[key] = expiry_counts.get(key, 0) + 1
        key = event.option_symbol or event.event_id
        contract_premium[key] = contract_premium.get(key, Decimal(0)) + premium(event)
    concentration = (max(contract_premium.values()) / total_premium
                     if total_premium and contract_premium else Decimal(0))
    latencies = [e.provider_latency_ms for e in rows if e.provider_latency_ms is not None]
    complete = sum(1 for e in rows if e.option_symbol and e.premium is not None and e.option_type)
    sweep_count = sum(e.is_sweep for e in rows)
    large_count = sum(premium(e) >= large_trade_threshold for e in rows)
    urgency = min(1.0, (sweep_count + large_count) / max(1, len(rows)))
    return InstitutionalFlowSnapshot(
        ticker=ticker.upper(), window_minutes=window_minutes,
        window_start=start, window_end=end,
        institutional_flow_status=status,
        institutional_flow_age_seconds=age, event_count=len(rows),
        net_call_premium=net_call, net_put_premium=net_put,
        net_directional_premium=directional,
        ask_side_call_premium=ask_call, ask_side_put_premium=ask_put,
        bid_side_call_premium=bid_call, bid_side_put_premium=bid_put,
        call_put_premium_ratio=float(call_total / put_total) if put_total else None,
        ask_bid_premium_ratio=float(ask_total / bid_total) if bid_total else None,
        sweep_count=sweep_count,
        block_count=sum(e.is_block for e in rows),
        multi_leg_count=sum(e.is_multi_leg for e in rows),
        large_trade_count=large_count,
        unique_contract_count=len({e.option_symbol for e in rows if e.option_symbol}),
        unique_expiration_count=len({e.expiration for e in rows if e.expiration}),
        short_dated_premium=total(lambda e: e.days_to_expiration is not None and e.days_to_expiration <= 7),
        same_day_expiration_premium=total(lambda e: e.days_to_expiration == 0),
        premium_velocity=float(total_premium / Decimal(str(max(window_minutes, 1)))),
        premium_acceleration=float(late / Decimal(str(half_minutes)) - early / Decimal(str(half_minutes))),
        flow_event_velocity=len(rows) / max(window_minutes, 1),
        repeated_strike_score=sum(v - 1 for v in strike_counts.values()) / max(1, len(rows)),
        repeated_expiration_score=sum(v - 1 for v in expiry_counts.values()) / max(1, len(rows)),
        bullish_flow_score=float(bullish / classified) if classified else None,
        bearish_flow_score=float(bearish / classified) if classified else None,
        flow_direction_score=float((bullish - bearish) / classified) if classified else None,
        flow_concentration_score=float(concentration),
        flow_divergence_score=None,
        opening_position_probability=None,
        institutional_urgency_score=urgency,
        data_quality_score=complete / len(rows),
        provider_latency_ms=sum(latencies) / len(latencies) if latencies else None,
    )


CONFIDENCE_WEIGHTS = {
    "version": "confidence-fusion-weights-v0",
    "institutional_flow": 0.20,
    "options_state": 0.25,
    "technical": 0.25,
    "liquidity": 0.15,
    "macro_catalyst": 0.10,
    "execution_quality": 0.05,
}
