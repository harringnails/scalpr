"""Fail-closed scope for operator-initiated manual PAPER option trades.

This module is deliberately separate from :mod:`scope_policy`.  The existing
SPY 0-2 DTE validator remains the default for every automated caller.  A server
endpoint must opt into this policy explicitly, and broker-side contract
validation is still required before an order can be submitted.
"""

from __future__ import annotations

import re
from datetime import date

import scope_policy


SCOPE_VERSION = "scalpr-manual-wide-options-0-60dte-v2"
MIN_DTE = 0
MAX_DTE = 60
SCOPE_VALIDATED = "validated"
SCOPE_OUT_OF_ENVELOPE = "manual_out_of_envelope"
UNDERLYING_RE = re.compile(r"^[A-Z]{1,6}$")


class ManualScopeError(ValueError):
    pass


def validate_underlying(underlying: str) -> str:
    """Validate ticker syntax only; Alpaca decides whether it is optionable."""
    normalized = str(underlying or "").strip().upper()
    if not UNDERLYING_RE.fullmatch(normalized):
        raise ManualScopeError(
            f"{normalized or 'empty symbol'} is not a supported equity ticker format."
        )
    return normalized


def validate_manual_option(symbol: str, as_of: date | None = None) -> dict:
    """Validate an OCC option and its 0-60 calendar-DTE manual envelope.

    This pure check does not claim the contract exists.  The manual server path
    must follow it with Alpaca ``get_asset`` and ``get_option_contract`` checks.
    """
    try:
        parsed = scope_policy.parse_occ(symbol)
    except scope_policy.ScopeError as exc:
        raise ManualScopeError(str(exc)) from exc
    validate_underlying(parsed["underlying"])
    today = as_of or scope_policy.market_date()
    dte = (parsed["expiry"] - today).days
    if not MIN_DTE <= dte <= MAX_DTE:
        raise ManualScopeError(
            f"Manual paper scope allows options with {MIN_DTE}-{MAX_DTE} DTE; "
            f"{parsed['symbol']} is {dte} DTE as of {today.isoformat()}."
        )
    return {
        **parsed,
        "dte": dte,
        "scope_version": SCOPE_VERSION,
        "scope_class": classify(parsed["symbol"], today),
    }


def classify(symbol: str, as_of: date | None = None) -> str:
    """Return the frozen-baseline class without weakening its validator."""
    try:
        scope_policy.validate_option(symbol, as_of)
    except scope_policy.ScopeError:
        return SCOPE_OUT_OF_ENVELOPE
    return SCOPE_VALIDATED


def validate_manual_trade(symbol: str, trade_type: str,
                          as_of: date | None = None) -> dict:
    if str(trade_type or "").lower() != "option":
        raise ManualScopeError(
            "The expanded manual paper builder supports options only; stock trades remain blocked."
        )
    return validate_manual_option(symbol, as_of)
