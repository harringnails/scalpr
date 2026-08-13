"""Credential-injected, read-only IVolatility adapter for Scalpr V2."""

from __future__ import annotations

import os
from typing import Optional

import requests


BASE_URL = "https://restapi.ivolatility.com"
EOD_CHAIN_PATH = "/equities/eod/stock-opts-by-param"
SOURCE_VERSION = "ivolatility-rest-v0"
ENV_KEY = "IVOLATILITY_API_KEY"


class IVolatilityError(RuntimeError):
    pass


class IVolatilityUnavailable(IVolatilityError):
    pass


class IVolatilityClient:
    """No broker dependency, no background loop, and no secret persistence."""

    def __init__(self, api_key: Optional[str] = None, *, session=None,
                 base_url: str = BASE_URL, timeout: float = 12.0):
        self._api_key = (api_key or "").strip()
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)

    @classmethod
    def from_environment(cls, **kwargs):
        return cls(os.getenv(ENV_KEY), **kwargs)

    def status(self) -> dict:
        return {
            "provider": "ivolatility",
            "source_version": SOURCE_VERSION,
            "configured": bool(self._api_key),
            "enabled": False,
            "mode": "read_only",
            "reason": "capture_not_operator_enabled" if self._api_key else "missing_environment_key",
            "credential_environment_variable": ENV_KEY,
        }

    def _get(self, path: str, params: dict) -> dict:
        if not self._api_key:
            raise IVolatilityUnavailable(
                f"IVolatility is disabled because {ENV_KEY} is not configured"
            )
        request_params = dict(params)
        request_params["apiKey"] = self._api_key
        try:
            response = self.session.get(
                self.base_url + path, params=request_params, timeout=self.timeout
            )
        except requests.RequestException as exc:
            # Never include a prepared URL: query authentication would expose the key.
            raise IVolatilityError("IVolatility request failed") from exc
        if response.status_code == 204:
            return {"status": {"code": "NO_DATA"}, "data": []}
        if response.status_code != 200:
            raise IVolatilityError(f"IVolatility API HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise IVolatilityError("IVolatility API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise IVolatilityError("IVolatility API returned an unexpected payload")
        return payload

    def fetch_eod_chain(self, *, symbol: str, trade_date: str,
                        dte_from: int = 0, dte_to: int = 2,
                        moneyness_from: float = -30,
                        moneyness_to: float = 30) -> dict:
        """Fetch a bounded EOD chain for both rights.

        Scalpr V2 currently fails closed outside SPY 0-2 DTE. The moneyness
        bounds are explicit because this IVolatility endpoint requires either
        a delta range or a moneyness range.
        """
        ticker = str(symbol).strip().upper()
        if ticker != "SPY" or dte_from != 0 or dte_to != 2:
            raise ValueError("Scalpr V2 IVolatility scope is SPY 0-2 DTE")
        if moneyness_from >= moneyness_to:
            raise ValueError("moneyness_from must be below moneyness_to")
        payloads = []
        for right in ("C", "P"):
            payloads.append(self._get(EOD_CHAIN_PATH, {
                "symbol": ticker,
                "tradeDate": str(trade_date),
                "dteFrom": dte_from,
                "dteTo": dte_to,
                "moneynessFrom": moneyness_from,
                "moneynessTo": moneyness_to,
                "cp": right,
                "region": "USA",
            }))
        status_codes = [str((p.get("status") or {}).get("code") or "OK").upper()
                        for p in payloads]
        if "PENDING" in status_codes:
            status = "pending"
        elif all(code == "NO_DATA" for code in status_codes):
            status = "no_data"
        else:
            status = "ready"
        return {
            "provider": "ivolatility",
            "source_version": SOURCE_VERSION,
            "endpoint": EOD_CHAIN_PATH,
            "query": {
                "symbol": ticker, "tradeDate": str(trade_date),
                "dteFrom": dte_from, "dteTo": dte_to,
                "moneynessFrom": moneyness_from, "moneynessTo": moneyness_to,
                "rights": ["C", "P"], "region": "USA",
            },
            "source_status": status,
            "payloads": payloads,
        }
