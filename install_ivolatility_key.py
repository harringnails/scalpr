#!/usr/bin/env python3
"""Validate and store an IVolatility API key without displaying it."""

from __future__ import annotations

import ctypes
import ctypes.util
import getpass
import hmac
import os
import subprocess
import sys
from datetime import date, timedelta

import requests


SERVICE = "scalpr.ivolatility.api"
VERIFY_URL = "https://restapi.ivolatility.com/equities/eod/stock-opts-by-param"
ERR_SEC_ITEM_NOT_FOUND = -25300


class KeychainError(RuntimeError):
    pass


def _security_framework():
    path = ctypes.util.find_library("Security")
    if not path:
        raise KeychainError("macOS Security.framework is unavailable")
    security = ctypes.CDLL(path)
    security.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemModifyAttributesAndData.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
    ]
    security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    security.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    return security


def write_keychain_secret(account: str, service: str, key: str) -> None:
    security = _security_framework()
    account_bytes, service_bytes, key_bytes = (
        account.encode(), service.encode(), key.encode())
    key_buffer = ctypes.create_string_buffer(key_bytes)
    item = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None, len(service_bytes), service_bytes,
        len(account_bytes), account_bytes, None, None, ctypes.byref(item))
    if status == 0:
        status = security.SecKeychainItemModifyAttributesAndData(
            item, None, len(key_bytes), ctypes.cast(key_buffer, ctypes.c_void_p))
    elif status == ERR_SEC_ITEM_NOT_FOUND:
        status = security.SecKeychainAddGenericPassword(
            None, len(service_bytes), service_bytes,
            len(account_bytes), account_bytes, len(key_bytes),
            ctypes.cast(key_buffer, ctypes.c_void_p), None)
    if status != 0:
        raise KeychainError(f"Keychain returned OSStatus {status}")


def normalize_key(value: str) -> str:
    key = (value or "").strip()
    if key.lower().startswith("apikey="):
        key = key.split("=", 1)[1].strip()
    if key.lower().startswith("api_key="):
        key = key.split("=", 1)[1].strip()
    return key


def _recent_weekday() -> str:
    day = date.today() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def validate_key(key: str, *, session=requests) -> tuple[bool, str]:
    params = {
        "apiKey": key, "symbol": "SPY", "tradeDate": _recent_weekday(),
        "dteFrom": 0, "dteTo": 2, "moneynessFrom": -10,
        "moneynessTo": 10, "cp": "C", "region": "USA",
    }
    try:
        response = session.get(VERIFY_URL, params=params, timeout=15)
    except requests.RequestException:
        return False, "Could not reach IVolatility. Nothing was stored."
    if response.status_code in {200, 204}:
        return True, "API key accepted by IVolatility."
    if response.status_code == 401:
        return False, "IVolatility rejected the API key (HTTP 401). Nothing was stored."
    if response.status_code == 403:
        return False, "Key recognized, but the required options endpoint is forbidden (HTTP 403). Nothing was stored."
    if response.status_code == 429:
        return False, "IVolatility rate-limited validation (HTTP 429). Nothing was stored."
    return False, f"IVolatility returned HTTP {response.status_code}. Nothing was stored."


def store_key(key: str) -> tuple[bool, str]:
    account = os.environ.get("USER") or getpass.getuser()
    try:
        write_keychain_secret(account, SERVICE, key)
        readback = subprocess.run(
            ["security", "find-generic-password", "-a", account,
             "-s", SERVICE, "-w"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=False)
        if readback.returncode != 0 or not hmac.compare_digest(
                normalize_key(readback.stdout), key):
            return False, "Keychain verification failed; the validated key was not stored."
    except (OSError, KeychainError):
        return False, "macOS Keychain could not be opened."
    return True, "API key stored securely in macOS Keychain."


def main() -> int:
    print("IVolatility API setup for Scalpr")
    print("Paste the raw API key below. Do not enter your account password.")
    key = normalize_key(getpass.getpass("API key (hidden): "))
    if not key:
        print("No key entered. Nothing was stored.")
        return 1
    if any(character.isspace() for character in key):
        print("The entry contains spaces and does not look like a raw API key. Nothing was stored.")
        return 1
    valid, message = validate_key(key)
    print(message)
    if not valid:
        return 1
    stored, message = store_key(key)
    print(message)
    return 0 if stored else 1


if __name__ == "__main__":
    sys.exit(main())
