"""Point-in-time option contract assembly for Entry Intelligence.

This module is deterministic research plumbing.  It has no broker, order,
position, liquidation, server, or Guard imports.  One contract is assembled by
OCC symbol from field-authoritative inputs; missing data stays missing and every
consumed field carries provenance.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import math
import re
from typing import Any
from zoneinfo import ZoneInfo

import feature_engine as fe


CONTRACT_ASSEMBLY_SCHEMA_VERSION = "entry-contract-assembly-record-v1"
CONTRACT_ASSEMBLY_VERSION = "entry-contract-assembly-v2"
DATA_STATES = {"FRESH", "STALE", "MISSING", "UNAVAILABLE", "UNUSABLE"}
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
ET = ZoneInfo("America/New_York")


def _utc(value: Any, *, default_timezone: ZoneInfo | timezone = timezone.utc) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_timezone)
    return parsed.astimezone(timezone.utc)


def parse_occ(symbol: str) -> tuple[str, date, str, float] | None:
    match = OCC.match(str(symbol or "").upper())
    if not match:
        return None
    underlying, ymd, right, strike = match.groups()
    try:
        expiry = datetime.strptime(ymd, "%y%m%d").date()
    except ValueError:
        return None
    return underlying, expiry, right, int(strike) / 1000.0


def black_scholes_delta(*, spot: float, strike: float, expiry: date,
                        decided_at: Any, iv: float, right: str,
                        annual_rate: float, expiry_time_et: str,
                        day_count: float) -> float | None:
    """Return signed delta using fractional time, including same-day expiry."""
    try:
        hour, minute = (int(part) for part in expiry_time_et.split(":", 1))
        expiry_at = datetime.combine(expiry, time(hour, minute), tzinfo=ET)
        seconds = (expiry_at.astimezone(timezone.utc) - _utc(decided_at)).total_seconds()
        spot, strike, iv = float(spot), float(strike), float(iv)
        if seconds <= 0 or spot <= 0 or strike <= 0 or iv <= 0 or day_count <= 0:
            return None
        years = seconds / (float(day_count) * 24.0 * 60.0 * 60.0)
        sigma_root_t = iv * math.sqrt(years)
        if sigma_root_t <= 0:
            return None
        d1 = ((math.log(spot / strike)
               + (float(annual_rate) + 0.5 * iv * iv) * years)
              / sigma_root_t)
        normal_cdf = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        delta = normal_cdf if str(right).upper() in {"C", "CALL"} else normal_cdf - 1.0
        return round(delta, 6)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


def contract_assembly_hash(config: dict) -> str:
    return fe.canonical_hash({
        "contract_assembly_version": CONTRACT_ASSEMBLY_VERSION,
        "config": config,
    })


def _field(*, value: Any, source: str, observed_at: Any, received_at: Any,
           state: str, decided_at: datetime, detail: str | None = None) -> dict:
    observed = _utc(observed_at, default_timezone=ET) if observed_at is not None else None
    received = _utc(received_at, default_timezone=ET) if received_at is not None else None
    final_state = state if state in DATA_STATES else "UNUSABLE"
    if ((observed is not None and observed > decided_at)
            or (received is not None and received > decided_at)):
        final_state = "UNUSABLE"
        value = None
        detail = "timestamp_after_decided_at_rejected"
        observed = None
        received = None
    return {
        "source": source,
        "observed_at": observed.isoformat() if observed else None,
        "received_at": received.isoformat() if received else None,
        "state": final_state,
        "value": value,
        **({"detail": detail} if detail else {}),
    }


def _same_session_state(observed_at: Any, decided_at: datetime) -> str:
    if observed_at is None:
        return "MISSING"
    observed = _utc(observed_at, default_timezone=ET)
    if observed > decided_at:
        return "UNUSABLE"
    return ("FRESH" if observed.astimezone(ET).date()
            == decided_at.astimezone(ET).date() else "STALE")


def assemble_contract(*, chain: dict, chain_observed_at: Any,
                      snapshot: dict, underlying: dict,
                      decided_at: Any, config: dict) -> dict:
    """Join one chain row and one fresh snapshot without cross-field imputation."""
    decided = _utc(decided_at)
    symbol = str(chain.get("symbol") or chain.get("option_symbol") or "").upper()
    parsed = parse_occ(symbol)
    received_at = snapshot.get("received_at") or decided
    quote_observed_at = snapshot.get("quote_observed_at")
    quote_age = None
    try:
        quote_age = (decided - _utc(quote_observed_at)).total_seconds()
    except Exception:
        pass
    bid, ask = snapshot.get("bid"), snapshot.get("ask")
    quote_valid = False
    try:
        quote_valid = float(bid) > 0 and float(ask) >= float(bid)
    except (TypeError, ValueError):
        pass
    if quote_observed_at is None:
        quote_state = "MISSING"
    elif quote_age is None or quote_age < 0:
        quote_state = "UNUSABLE"
    elif quote_age > float(config["max_quote_age_seconds"]):
        quote_state = "STALE"
    elif not quote_valid:
        quote_state = "UNUSABLE"
    else:
        quote_state = "FRESH"

    chain_state = _same_session_state(chain_observed_at, decided)
    volume = chain.get("volume")
    open_interest = chain.get("open_interest", chain.get("oi"))
    try:
        volume = int(volume) if volume is not None else None
    except (TypeError, ValueError):
        volume = None
    try:
        open_interest = int(open_interest) if open_interest is not None else None
    except (TypeError, ValueError):
        open_interest = None
    volume_state = chain_state if volume is not None and volume >= 0 else "MISSING"
    oi_state = chain_state if open_interest is not None and open_interest >= 0 else "MISSING"

    iv = chain.get("iv")
    try:
        iv = float(iv) if iv is not None else None
    except (TypeError, ValueError):
        iv = None
    iv_state = chain_state if iv is not None and iv > 0 else "MISSING"

    metadata_state = "FRESH" if parsed else "UNUSABLE"
    if parsed:
        underlying_symbol, expiry, right, strike = parsed
        dte = max(0, (expiry - decided.astimezone(ET).date()).days)
    else:
        underlying_symbol, expiry, right, strike, dte = "", None, "", None, None

    underlying_spot = underlying.get("spot")
    underlying_observed_at = underlying.get("observed_at")
    underlying_received_at = underlying.get("received_at") or decided
    try:
        underlying_spot = float(underlying_spot)
        underlying_age = (decided - _utc(underlying_observed_at)).total_seconds()
        underlying_state = (
            "FRESH" if underlying_spot > 0 and 0 <= underlying_age
            <= float(config["local_bs"]["max_underlying_age_seconds"])
            else "STALE")
    except (TypeError, ValueError, AttributeError):
        underlying_spot, underlying_state = None, "MISSING"

    vendor_delta = snapshot.get("delta")
    try:
        vendor_delta = float(vendor_delta) if vendor_delta is not None else None
    except (TypeError, ValueError):
        vendor_delta = None
    if vendor_delta is not None and quote_state == "FRESH":
        delta = vendor_delta
        delta_source = "alpaca_snapshot_greeks"
        delta_state = "FRESH"
        delta_observed_at = quote_observed_at
        delta_detail = "greeks_timestamp_basis=co_returned_quote_timestamp"
    elif (parsed and underlying_state == "FRESH" and iv_state == "FRESH"
          and float(config["local_bs"]["min_iv"])
          <= float(iv) <= float(config["local_bs"]["max_iv"])):
        delta = black_scholes_delta(
            spot=underlying_spot, strike=strike, expiry=expiry,
            decided_at=decided, iv=iv, right=right,
            annual_rate=float(config["local_bs"]["annual_rate"]),
            expiry_time_et=str(config["local_bs"]["expiry_time_et"]),
            day_count=float(config["local_bs"]["day_count"]),
        )
        delta_source = "local_black_scholes"
        delta_state = "FRESH" if delta is not None else "UNAVAILABLE"
        delta_observed_at = max(
            _utc(underlying_observed_at), _utc(chain_observed_at, default_timezone=ET))
        delta_detail = "derived_from=fresh_underlying+same_session_chain_iv"
    else:
        delta, delta_source, delta_state = None, "none", "UNAVAILABLE"
        delta_observed_at = None
        delta_detail = "vendor_greeks_missing_and_local_bs_inputs_not_fresh"

    fields = {
        "metadata": _field(
            value={"underlying": underlying_symbol, "expiry": expiry.isoformat() if expiry else None,
                   "right": right, "strike": strike, "dte": dte},
            source="occ_symbol", observed_at=chain_observed_at,
            received_at=chain_observed_at, state=metadata_state, decided_at=decided),
        "bid": _field(value=bid, source="alpaca_opra_latest_quote",
                      observed_at=quote_observed_at, received_at=received_at,
                      state=quote_state, decided_at=decided),
        "ask": _field(value=ask, source="alpaca_opra_latest_quote",
                      observed_at=quote_observed_at, received_at=received_at,
                      state=quote_state, decided_at=decided),
        "volume": _field(value=volume, source="unusual_whales_option_chain",
                         observed_at=chain_observed_at, received_at=chain_observed_at,
                         state=volume_state, decided_at=decided,
                         detail="snapshot_daily_bar_volume_ignored"),
        "open_interest": _field(
            value=open_interest, source="unusual_whales_option_chain",
            observed_at=chain_observed_at, received_at=chain_observed_at,
            state=oi_state, decided_at=decided),
        "iv": _field(value=iv, source="unusual_whales_option_chain",
                     observed_at=chain_observed_at, received_at=chain_observed_at,
                     state=iv_state, decided_at=decided),
        "underlying": _field(
            value=underlying_spot, source=str(underlying.get("source") or "alpaca_underlying_bar"),
            observed_at=underlying_observed_at, received_at=underlying_received_at,
            state=underlying_state, decided_at=decided),
        "delta": _field(value=delta, source=delta_source,
                        observed_at=delta_observed_at, received_at=received_at,
                        state=delta_state, decided_at=decided, detail=delta_detail),
    }
    assembly_hash = contract_assembly_hash(config)
    return {
        "schema_version": CONTRACT_ASSEMBLY_SCHEMA_VERSION,
        "contract_assembly_version": CONTRACT_ASSEMBLY_VERSION,
        "contract_assembly_hash": assembly_hash,
        "option_symbol": symbol,
        "option_type": "CALL" if right == "C" else "PUT" if right == "P" else None,
        "expiry": expiry.isoformat() if expiry else chain.get("expiry"),
        "dte": dte,
        "strike": strike,
        "delta": fields["delta"]["value"],
        "delta_source": delta_source,
        "bid": fields["bid"]["value"],
        "ask": fields["ask"]["value"],
        "volume": fields["volume"]["value"],
        "open_interest": fields["open_interest"]["value"],
        "quote_observed_at": fields["bid"]["observed_at"],
        "quote_received_at": fields["bid"]["received_at"],
        "field_provenance": fields,
        "ignored_snapshot_volume": {
            "value": snapshot.get("snapshot_volume"),
            "state": "MISSING" if snapshot.get("snapshot_volume") in {None, 0, 0.0} else "UNUSABLE",
            "reason": "snapshot_volume_is_not_an_authoritative_session_volume_source",
        },
    }
