#!/usr/bin/env python3
"""Point-in-time option scenario arithmetic with no trading authority."""

from __future__ import annotations

import getpass
import math
import re
import subprocess
from datetime import date, datetime, time, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests


ET = ZoneInfo("America/New_York")
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
SCENARIO_MOVES = (-2.0, -1.0, -0.5, 0.5, 1.0, 2.0)
FLASHALPHA_KEYCHAIN_SERVICE = "scalpr.flashalpha.api"
FLASHALPHA_URL = "https://lab.flashalpha.com/optionquote/{underlying}"
MAX_QUOTE_AGE_SECONDS = 60.0
MAX_FLASH_AGE_SECONDS = 300.0


def parse_occ(symbol: str) -> tuple[str, date, str, float] | None:
    match = OCC.fullmatch(str(symbol or "").strip().upper())
    if not match:
        return None
    underlying, ymd, right, strike = match.groups()
    try:
        expiry = datetime.strptime(ymd, "%y%m%d").date()
    except ValueError:
        return None
    return underlying, expiry, right, int(strike) / 1000.0


def parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_iv(value: Any) -> float | None:
    number = finite(value)
    if number is None or number <= 0:
        return None
    return number / 100.0 if number > 3.0 else number


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def bs_price_greeks(
    *, spot: float, strike: float, years: float, iv: float, right: str,
) -> dict[str, float] | None:
    if min(spot, strike, years, iv) <= 0:
        return None
    sigma_root = iv * math.sqrt(years)
    if sigma_root <= 0:
        return None
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years) / sigma_root
    d2 = d1 - sigma_root
    if right == "C":
        price = spot * normal_cdf(d1) - strike * normal_cdf(d2)
        delta = normal_cdf(d1)
    else:
        price = strike * normal_cdf(-d2) - spot * normal_cdf(-d1)
        delta = normal_cdf(d1) - 1.0
    gamma = normal_pdf(d1) / (spot * sigma_root)
    return {"price": price, "delta": delta, "gamma": gamma}


def implied_volatility(
    *, option_mid: float, spot: float, strike: float, years: float, right: str,
) -> float | None:
    intrinsic = max(spot - strike, 0.0) if right == "C" else max(strike - spot, 0.0)
    upper = spot if right == "C" else strike
    if years <= 0 or option_mid <= intrinsic or option_mid >= upper:
        return None
    low, high = 0.0001, 5.0
    high_price = bs_price_greeks(spot=spot, strike=strike, years=years, iv=high, right=right)
    if high_price is None or high_price["price"] < option_mid:
        return None
    for _ in range(100):
        mid = (low + high) / 2.0
        model = bs_price_greeks(spot=spot, strike=strike, years=years, iv=mid, right=right)
        if model is None:
            return None
        if model["price"] < option_mid:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def time_to_expiry_years(expiry: date, observed_at: datetime) -> float:
    expiry_at = datetime.combine(expiry, time(16, 0), tzinfo=ET).astimezone(timezone.utc)
    return max(0.0, (expiry_at - observed_at.astimezone(timezone.utc)).total_seconds()) / (365.0 * 86400.0)


def calculate_scenario(
    *, option_symbol: str, quantity: int, spot: Any, bid: Any, ask: Any,
    observed_at: datetime, flash_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = {
        "admission_authority": False,
        "execution_authority": False,
        "is_forecast": False,
        "label": "mechanical scenarios given an assumed move — not a forecast or a trade signal.",
        "option_symbol": option_symbol.upper(),
        "quantity": quantity,
        "scenario_moves_points": list(SCENARIO_MOVES),
    }
    parsed = parse_occ(option_symbol)
    spot_value, bid_value, ask_value = finite(spot), finite(bid), finite(ask)
    if parsed is None:
        return {**base, "status": "UNAVAILABLE", "reason": "INVALID_OCC_SYMBOL", "breakeven": None,
                "expected_move": None, "scenario_rows": []}
    underlying, expiry, right, strike = parsed
    if quantity < 1:
        return {**base, "status": "UNAVAILABLE", "reason": "INVALID_QUANTITY", "breakeven": None,
                "expected_move": None, "scenario_rows": []}
    quote_clean = bid_value is not None and ask_value is not None and bid_value > 0 and ask_value >= bid_value
    premium = ask_value if quote_clean else None
    option_mid = (bid_value + ask_value) / 2.0 if quote_clean else None
    breakeven_price = strike + premium if premium is not None and right == "C" else strike - premium if premium is not None else None
    breakeven = None if breakeven_price is None or not spot_value else {
        "premium_basis": "live_executable_ask",
        "premium_points": round(premium, 6),
        "price": round(breakeven_price, 6),
        "underlying_move_pct": round((breakeven_price / spot_value - 1.0) * 100.0, 6),
    }
    years = time_to_expiry_years(expiry, observed_at)
    inverted_iv = None
    if option_mid is not None and spot_value and years > 0:
        inverted_iv = implied_volatility(
            option_mid=option_mid, spot=spot_value, strike=strike, years=years, right=right,
        )

    flash = flash_contract or {}
    flash_fresh = flash.get("status") == "FRESH"
    flash_iv = normalize_iv(flash.get("iv")) if flash_fresh else None
    selected_iv = flash_iv if flash_iv is not None else inverted_iv
    iv_source = "FlashAlpha:optionquote:strike" if flash_iv is not None else "black_scholes_inversion:live_opra_mid" if inverted_iv is not None else None
    expected_move = None
    if selected_iv is not None and spot_value and years > 0:
        points = spot_value * selected_iv * math.sqrt(years)
        expected_move = {
            "implied_volatility": round(selected_iv, 8),
            "iv_source": iv_source,
            "lower": round(spot_value - points, 6),
            "points": round(points, 6),
            "upper": round(spot_value + points, 6),
        }

    delta = finite(flash.get("delta")) if flash_fresh else None
    gamma = finite(flash.get("gamma")) if flash_fresh else None
    greek_source = "FlashAlpha:optionquote:strike" if delta is not None and gamma is not None else None
    if greek_source is None and inverted_iv is not None and spot_value and years > 0:
        derived = bs_price_greeks(
            spot=spot_value, strike=strike, years=years, iv=inverted_iv, right=right,
        )
        if derived:
            delta, gamma = derived["delta"], derived["gamma"]
            greek_source = "black_scholes:inverted_live_opra_mid"

    rows = []
    for move in SCENARIO_MOVES:
        if delta is None or gamma is None or option_mid is None:
            rows.append({"underlying_move_points": move, "status": "UNAVAILABLE",
                         "option_change_points": None, "pnl_pct": None, "pnl_dollars": None})
            continue
        option_change = delta * move + 0.5 * gamma * move * move
        rows.append({
            "option_change_points": round(option_change, 6),
            "pnl_dollars": round(option_change * quantity * 100.0, 2),
            "pnl_pct": round(option_change / option_mid * 100.0, 4),
            "status": "AVAILABLE",
            "underlying_move_points": move,
        })

    return {
        **base,
        "breakeven": breakeven,
        "expected_move": expected_move,
        "greeks": {
            "delta": round(delta, 8) if delta is not None else None,
            "gamma": round(gamma, 8) if gamma is not None else None,
            "source": greek_source,
        },
        "option_mid": round(option_mid, 6) if option_mid is not None else None,
        "right": "CALL" if right == "C" else "PUT",
        "scenario_rows": rows,
        "spot": spot_value,
        "status": "AVAILABLE" if breakeven is not None else "UNAVAILABLE",
        "strike": strike,
        "time_to_expiry_years": round(years, 12),
        "underlying": underlying,
    }


def load_keychain_secret(
    service: str = FLASHALPHA_KEYCHAIN_SERVICE,
    *, account: str | None = None, runner: Callable[..., Any] = subprocess.run,
) -> str | None:
    command = ["security", "find-generic-password"]
    if account:
        command.extend(["-a", account])
    command.extend(["-s", service, "-w"])
    result = runner(command, capture_output=True, text=True, check=False)
    value = (result.stdout or "").strip()
    return value if result.returncode == 0 and value else None


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("contracts", "options", "data", "strikes"):
            if isinstance(payload.get(key), list):
                return [row for row in payload[key] if isinstance(row, dict)]
    return []


def extract_flash_contract(
    payload: Any, *, strike: float, right: str, observed_at: datetime,
) -> dict[str, Any] | None:
    prefix = "call" if right == "C" else "put"
    for row in _rows(payload):
        row_strike = finite(row.get("strike"))
        row_right = str(row.get("type") or row.get("right") or "").upper()[:1]
        if row_strike is None or abs(row_strike - strike) > 1e-9:
            continue
        if row_right and row_right != right:
            continue
        provider_ts = parse_timestamp(
            row.get("lastUpdate") or row.get("last_update") or row.get("updated_at")
            or (payload.get("as_of") if isinstance(payload, dict) else None)
        )
        age = (observed_at - provider_ts).total_seconds() if provider_ts else None
        status = "FRESH" if age is not None and 0 <= age <= MAX_FLASH_AGE_SECONDS else "STALE_OR_MISSING"
        return {
            "age_seconds": round(age, 6) if age is not None else None,
            "delta": row.get(f"{prefix}_delta", row.get("delta")),
            "gamma": row.get(f"{prefix}_gamma", row.get("gamma")),
            "iv": row.get(f"{prefix}_iv", row.get("iv")),
            "provider_timestamp_utc": provider_ts.isoformat() if provider_ts else None,
            "status": status,
        }
    return None


class LiveScenarioSource:
    """Read-only Alpaca and FlashAlpha clients; no trading client is constructed."""

    def __init__(self, api_key: str, api_secret: str, *, http: Any = requests) -> None:
        from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
        self.option_client = OptionHistoricalDataClient(api_key, api_secret)
        self.stock_client = StockHistoricalDataClient(api_key, api_secret)
        self.http = http

    def fetch(self, option_symbol: str, quantity: int, observed_at: datetime) -> dict[str, Any]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestQuoteRequest

        parsed = parse_occ(option_symbol)
        if parsed is None:
            return calculate_scenario(
                option_symbol=option_symbol, quantity=quantity, spot=None, bid=None, ask=None,
                observed_at=observed_at,
            )
        underlying, expiry, right, strike = parsed
        option = self.option_client.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=option_symbol)
        ).get(option_symbol)
        stock = self.stock_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=underlying, feed=DataFeed.SIP)
        ).get(underlying)
        option_ts = parse_timestamp(getattr(option, "timestamp", None))
        stock_ts = parse_timestamp(getattr(stock, "timestamp", None))
        option_age = (observed_at - option_ts).total_seconds() if option_ts else None
        stock_age = (observed_at - stock_ts).total_seconds() if stock_ts else None
        quote_fresh = option_age is not None and 0 <= option_age <= MAX_QUOTE_AGE_SECONDS
        spot_fresh = stock_age is not None and 0 <= stock_age <= MAX_QUOTE_AGE_SECONDS
        bid = getattr(option, "bid_price", None) if quote_fresh else None
        ask = getattr(option, "ask_price", None) if quote_fresh else None
        spot = None
        if spot_fresh:
            stock_bid, stock_ask = finite(getattr(stock, "bid_price", None)), finite(getattr(stock, "ask_price", None))
            if stock_bid and stock_ask and stock_ask >= stock_bid:
                spot = (stock_bid + stock_ask) / 2.0

        flash_contract = None
        flash_key = load_keychain_secret(account=getpass.getuser())
        if flash_key:
            try:
                response = self.http.get(
                    FLASHALPHA_URL.format(underlying=underlying),
                    params={"expiry": expiry.isoformat()},
                    headers={"X-Api-Key": flash_key, "Accept": "application/json"}, timeout=12,
                )
                if response.status_code == 200:
                    flash_contract = extract_flash_contract(
                        response.json(), strike=strike, right=right, observed_at=observed_at,
                    )
            except (requests.RequestException, ValueError):
                # FlashAlpha is optional: preserve breakeven and BS fallback from live quotes.
                flash_contract = None
        result = calculate_scenario(
            option_symbol=option_symbol, quantity=quantity, spot=spot, bid=bid, ask=ask,
            observed_at=observed_at, flash_contract=flash_contract,
        )
        result["provenance"] = {
            "flashalpha": flash_contract or {"status": "UNAVAILABLE"},
            "option_quote": {"age_seconds": option_age, "provider_timestamp_utc": option_ts.isoformat() if option_ts else None,
                             "source": "Alpaca:OPRA", "status": "FRESH" if quote_fresh else "STALE_OR_MISSING"},
            "spot_quote": {"age_seconds": stock_age, "provider_timestamp_utc": stock_ts.isoformat() if stock_ts else None,
                           "source": "Alpaca:SIP", "status": "FRESH" if spot_fresh else "STALE_OR_MISSING"},
        }
        return result
