"""Versioned scope for Wave Riding research simulations only.

This does not alter Standard-mode trading scope. Cohort A remains SPY 0-2 DTE;
SPX/SPXW simulations use Alpaca native index values and a separate Cohort E.
"""

from __future__ import annotations

from datetime import date
import re

import scope_policy as standard_scope


SCOPE_VERSION = "wave-shadow-native-spx-index-scope-v5"
VOO_CALL_COHORT_ID = "wave-riding-v0-voo-call-observation-cohort-b"
QQQ_COHORT_ID = "wave-riding-v0-qqq-observation-cohort-c"
MULTI_UNDERLYING_COHORT_ID = "wave-riding-v0-multi-underlying-observation-cohort-d"
SPX_INDEX_COHORT_ID = "wave-riding-v0-spx-index-observation-cohort-e"
TICKER_RE = re.compile(r"^[A-Z]{1,6}$")
SUPPORTED_INDEX_UNDERLYINGS = frozenset({"SPX", "SPXW"})
UNSUPPORTED_INDEX_UNDERLYINGS = frozenset({"NDX", "NDXP", "RUT", "VIX"})


def is_native_index(underlying: str) -> bool:
    return str(underlying or "").strip().upper() in SUPPORTED_INDEX_UNDERLYINGS


def validate_underlying(underlying: str) -> str:
    normalized = str(underlying or "").strip().upper()
    if not TICKER_RE.fullmatch(normalized):
        raise standard_scope.ScopeError(
            f"{normalized or 'empty symbol'} is not a valid equity ticker for {SCOPE_VERSION}."
        )
    if normalized in UNSUPPORTED_INDEX_UNDERLYINGS:
        raise standard_scope.ScopeError(
            f"{normalized} is an index. Wave shadow simulation requires a live Alpaca "
            "equity/ETF quote and minute bars for synchronized ATR; use Workup for "
            f"{normalized} evidence, but it cannot enter this Wave study."
        )
    # SPXW is the weekly option root; the underlying data series is SPX.
    return "SPX" if normalized == "SPXW" else normalized


def validate_option(symbol: str, as_of: date | None = None) -> dict:
    parsed = standard_scope.parse_occ(symbol)
    contract_underlying = parsed["underlying"]
    underlying = validate_underlying(contract_underlying)
    today = as_of or standard_scope.market_date()
    dte = (parsed["expiry"] - today).days

    if underlying == "SPY":
        if not 0 <= dte <= 2:
            raise standard_scope.ScopeError(
                f"Wave shadow SPY scope allows 0-2 DTE; {parsed['symbol']} is {dte} DTE."
            )
        cohort_id = "wave-riding-v0-observation-cohort-a"
    elif underlying == "SPX":
        if not 0 <= dte <= 60:
            raise standard_scope.ScopeError(
                f"Wave shadow SPX/SPXW scope allows 0-60 DTE; {parsed['symbol']} is {dte} DTE."
            )
        cohort_id = SPX_INDEX_COHORT_ID
    elif underlying == "VOO":
        if parsed["right"] != "C":
            raise standard_scope.ScopeError("Wave shadow VOO scope allows CALL contracts only.")
        if not 7 <= dte <= 60:
            raise standard_scope.ScopeError(
                f"Wave shadow VOO CALL scope allows 7-60 DTE; {parsed['symbol']} is {dte} DTE."
            )
        cohort_id = VOO_CALL_COHORT_ID
    elif underlying == "QQQ":
        if not 7 <= dte <= 60:
            raise standard_scope.ScopeError(
                f"Wave shadow QQQ scope allows 7-60 DTE; {parsed['symbol']} is {dte} DTE."
            )
        cohort_id = QQQ_COHORT_ID
    else:
        if not 7 <= dte <= 60:
            raise standard_scope.ScopeError(
                f"Wave shadow research scope allows 7-60 DTE; {parsed['symbol']} is {dte} DTE."
            )
        cohort_id = MULTI_UNDERLYING_COHORT_ID

    return {**parsed, "contract_underlying": contract_underlying,
            "underlying": underlying, "dte": dte, "scope_version": SCOPE_VERSION,
            "research_cohort_id": cohort_id}


def validate_contract_direction(symbol: str, direction: str,
                                as_of: date | None = None) -> dict:
    parsed = validate_option(symbol, as_of)
    normalized_direction = str(direction or "").strip().upper()
    expected = "CALL" if parsed["right"] == "C" else "PUT"
    if normalized_direction != expected:
        raise standard_scope.ScopeError(
            f"Direction {normalized_direction or 'empty'} does not match {parsed['symbol']} ({expected})."
        )
    return parsed
