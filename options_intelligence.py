"""Provider-neutral, deterministic options-intelligence contracts for Scalpr V2.

This module is research-only.  It has no broker imports and no order path.
Missing source fields remain missing; they are never imputed into evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


SNAPSHOT_SCHEMA_VERSION = "scalpr-v2-options-snapshot-v0"
FEATURE_SCHEMA_VERSION = "scalpr-v2-options-features-v0"
ALLOWED_UNDERLYINGS = frozenset({"SPY"})
MIN_DTE = 0
MAX_DTE = 2


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_int(value: Any) -> Optional[int]:
    number = _finite_float(value)
    if number is None or number < 0:
        return None
    return int(number)


def _first(row: dict, *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class OptionContractSnapshot:
    provider: str
    underlying: str
    option_symbol: str
    provider_timestamp: Optional[str]
    expiration_date: Optional[str]
    dte: int
    right: str
    strike: Optional[float]
    underlying_price: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    volume: Optional[int]
    open_interest: Optional[int]
    iv: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]
    theta: Optional[float]
    vega: Optional[float]
    missing_fields: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_ivolatility_row(row: dict, *, underlying: str) -> OptionContractSnapshot:
    """Normalize one IVolatility row without guessing absent values."""
    symbol = str(underlying).strip().upper()
    if symbol not in ALLOWED_UNDERLYINGS:
        raise ValueError(f"options V2 scope permits only {sorted(ALLOWED_UNDERLYINGS)}")
    dte_value = _nonnegative_int(_first(row, "dte", "DTE"))
    if dte_value is None or not MIN_DTE <= dte_value <= MAX_DTE:
        raise ValueError(f"DTE must be between {MIN_DTE} and {MAX_DTE}")
    right = str(_first(row, "call_put", "callPut", "cp") or "").upper()
    if right not in {"C", "P"}:
        raise ValueError("option right must be C or P")

    values = {
        "strike": _finite_float(_first(row, "price_strike", "strike", "strikePrice")),
        "underlying_price": _finite_float(_first(row, "underlying_price", "underlyingPrice")),
        "bid": _finite_float(_first(row, "Bid", "bid")),
        "ask": _finite_float(_first(row, "Ask", "ask")),
        "iv": _finite_float(_first(row, "iv", "IV", "impliedVolatility")),
        "delta": _finite_float(_first(row, "delta", "Delta")),
        "gamma": _finite_float(_first(row, "gamma", "Gamma")),
        "theta": _finite_float(_first(row, "theta", "Theta")),
        "vega": _finite_float(_first(row, "vega", "Vega")),
    }
    volume = _nonnegative_int(_first(row, "volume", "Volume"))
    open_interest = _nonnegative_int(
        _first(row, "openinterest", "open_interest", "openInterest", "oi")
    )
    required_evidence = {
        "strike": values["strike"], "underlying_price": values["underlying_price"],
        "bid": values["bid"], "ask": values["ask"], "volume": volume,
        "open_interest": open_interest, "iv": values["iv"],
        "delta": values["delta"], "gamma": values["gamma"],
        "theta": values["theta"], "vega": values["vega"],
    }
    missing = tuple(sorted(name for name, value in required_evidence.items() if value is None))
    return OptionContractSnapshot(
        provider="ivolatility",
        underlying=symbol,
        option_symbol=str(_first(row, "option_symbol", "optionSymbol", "symbol") or ""),
        provider_timestamp=_first(row, "timestamp", "c_date", "tradeDate"),
        expiration_date=_first(row, "expiration_date", "expirationDate", "expiry"),
        dte=dte_value,
        right=right,
        volume=volume,
        open_interest=open_interest,
        missing_fields=missing,
        **values,
    )


def build_chain_snapshot(*, provider: str, underlying: str, captured_at: str,
                         contracts: list[OptionContractSnapshot],
                         raw_payload_hash: str, source_status: str = "ready") -> dict:
    symbol = str(underlying).upper()
    if symbol not in ALLOWED_UNDERLYINGS:
        raise ValueError(f"options V2 scope permits only {sorted(ALLOWED_UNDERLYINGS)}")
    rows = sorted(
        (contract.as_dict() for contract in contracts),
        key=lambda row: (row["dte"], row["expiration_date"] or "", row["strike"] or -1,
                         row["right"], row["option_symbol"]),
    )
    content = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "provider": provider,
        "underlying": symbol,
        "captured_at": captured_at,
        "scope": {"min_dte": MIN_DTE, "max_dte": MAX_DTE},
        "source_status": source_status,
        "raw_payload_hash": raw_payload_hash,
        "contracts": rows,
    }
    content["snapshot_id"] = canonical_hash(content)
    return content


def engineer_options_features(snapshot: dict) -> dict:
    """Build deterministic descriptive features; never emits a trade signal."""
    contracts = snapshot.get("contracts") or []
    calls = [row for row in contracts if row.get("right") == "C"]
    puts = [row for row in contracts if row.get("right") == "P"]

    def total(rows: list[dict], key: str) -> Optional[int]:
        values = [row.get(key) for row in rows if row.get(key) is not None]
        return sum(values) if values else None

    def weighted_iv(rows: list[dict]) -> Optional[float]:
        usable = [(row.get("iv"), row.get("open_interest")) for row in rows]
        usable = [(iv, oi) for iv, oi in usable if iv is not None and oi is not None and oi > 0]
        weight = sum(oi for _, oi in usable)
        return sum(iv * oi for iv, oi in usable) / weight if weight else None

    call_oi, put_oi = total(calls, "open_interest"), total(puts, "open_interest")
    call_volume, put_volume = total(calls, "volume"), total(puts, "volume")
    call_iv, put_iv = weighted_iv(calls), weighted_iv(puts)
    spreads = []
    for row in contracts:
        bid, ask = row.get("bid"), row.get("ask")
        if bid is not None and ask is not None and ask >= bid and (ask + bid) > 0:
            spreads.append((ask - bid) / ((ask + bid) / 2.0))
    unsigned_gamma = sum(
        abs(row["gamma"]) * row["open_interest"] * 100
        for row in contracts
        if row.get("gamma") is not None and row.get("open_interest") is not None
    ) if contracts else None
    features = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "snapshot_id": snapshot.get("snapshot_id"),
        "underlying": snapshot.get("underlying"),
        "captured_at": snapshot.get("captured_at"),
        "contract_count": len(contracts),
        "call_open_interest": call_oi,
        "put_open_interest": put_oi,
        "put_call_open_interest_ratio": (put_oi / call_oi) if call_oi and put_oi is not None else None,
        "call_volume": call_volume,
        "put_volume": put_volume,
        "put_call_volume_ratio": (put_volume / call_volume) if call_volume and put_volume is not None else None,
        "call_oi_weighted_iv": call_iv,
        "put_oi_weighted_iv": put_iv,
        "put_call_iv_skew": (put_iv - call_iv) if call_iv is not None and put_iv is not None else None,
        "median_relative_bid_ask_spread": (
            sorted(spreads)[len(spreads) // 2] if spreads else None
        ),
        "unsigned_gamma_concentration": unsigned_gamma,
        "dealer_gamma_exposure": None,
        "gamma_flip_distance": None,
        "dealer_hedge_pressure": None,
        "dealer_positioning_status": "unavailable_without_position_sign_assumption",
        "recommendation": None,
        "qualifying": False,
        "disclaimer": "Research evidence only; no execution or position sizing authority.",
    }
    features["feature_id"] = canonical_hash(features)
    return features


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
