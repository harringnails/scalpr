#!/usr/bin/env python3
"""Fail-closed, read-only Alpaca PAPER flat-account proof for cold startup."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

import requests


BASE_URL = "https://paper-api.alpaca.markets/v2"


def prove_flat(*, api_key: str, api_secret: str,
               requester: Callable[..., Any] = requests.get) -> dict[str, Any]:
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
    account = requester(f"{BASE_URL}/account", headers=headers, timeout=15)
    positions = requester(f"{BASE_URL}/positions", headers=headers, timeout=15)
    orders = requester(f"{BASE_URL}/orders", params={"status": "open", "limit": 500},
                       headers=headers, timeout=15)
    for response in (account, positions, orders):
        response.raise_for_status()
    account_value, position_values, order_values = account.json(), positions.json(), orders.json()
    active = str(account_value.get("status", "")).upper() == "ACTIVE"
    flat = active and isinstance(position_values, list) and not position_values and isinstance(order_values, list) and not order_values
    return {"schema_version": "scalpr-cold-start-flat-proof-v0",
            "source": "alpaca_trading_api_direct_uncached", "mode": "paper",
            "account_status": account_value.get("status"),
            "positions_count": len(position_values) if isinstance(position_values, list) else None,
            "open_orders_count": len(order_values) if isinstance(order_values, list) else None,
            "flat": flat}


def main() -> int:
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        print(json.dumps({"flat": False, "mode": "paper", "status": "CREDENTIALS_UNAVAILABLE"}))
        return 2
    try:
        proof = prove_flat(api_key=key, api_secret=secret)
    except Exception as exc:
        print(json.dumps({"flat": False, "mode": "paper", "status": "PROOF_UNAVAILABLE",
                          "error_type": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps(proof, sort_keys=True))
    return 0 if proof["flat"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
