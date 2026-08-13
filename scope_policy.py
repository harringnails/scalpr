"""Fail-closed product scope for the paper/shadow Scalpr baseline."""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo


SCOPE_VERSION = "scalpr-spy-options-0-2dte-v1"
ALLOWED_UNDERLYING = "SPY"
MIN_DTE = 0
MAX_DTE = 2
OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def market_date() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


class ScopeError(ValueError):
    pass


def parse_occ(symbol: str) -> dict:
    normalized = str(symbol or "").strip().upper()
    match = OCC_RE.fullmatch(normalized)
    if not match:
        raise ScopeError(f"{normalized or 'empty symbol'} is not a full OCC option symbol")
    underlying, expiry_text, right, strike_text = match.groups()
    expiry = datetime.strptime(expiry_text, "%y%m%d").date()
    return {
        "symbol": normalized,
        "underlying": underlying,
        "expiry": expiry,
        "right": right,
        "strike": int(strike_text) / 1000.0,
    }


def validate_underlying(underlying: str) -> str:
    normalized = str(underlying or "").strip().upper()
    if normalized != ALLOWED_UNDERLYING:
        raise ScopeError(
            f"V2 scope is {ALLOWED_UNDERLYING} only; {normalized or 'empty symbol'} is blocked "
            f"by {SCOPE_VERSION}."
        )
    return normalized


def validate_option(symbol: str, as_of: date | None = None) -> dict:
    parsed = parse_occ(symbol)
    validate_underlying(parsed["underlying"])
    today = as_of or market_date()
    dte = (parsed["expiry"] - today).days
    if not MIN_DTE <= dte <= MAX_DTE:
        raise ScopeError(
            f"V2 scope allows {ALLOWED_UNDERLYING} options with {MIN_DTE}-{MAX_DTE} DTE; "
            f"{parsed['symbol']} is {dte} DTE as of {today.isoformat()}."
        )
    return {**parsed, "dte": dte, "scope_version": SCOPE_VERSION}


def validate_trade(symbol: str, trade_type: str, as_of: date | None = None) -> dict:
    if str(trade_type or "stock").lower() == "option":
        return validate_option(symbol, as_of)
    raise ScopeError(
        f"V2 scope is {ALLOWED_UNDERLYING} options only with {MIN_DTE}-{MAX_DTE} DTE; "
        "stock trades are blocked."
    )
