#!/usr/bin/env python3
"""Validate and store Alpaca paper API credentials without displaying them."""

from __future__ import annotations

import ctypes
import ctypes.util
import getpass
import hmac
import os
import subprocess
import sys

import requests


SERVICE_KEY = "scalpr.alpaca.paper.key"
SERVICE_SECRET = "scalpr.alpaca.paper.secret"
VERIFY_URL = "https://paper-api.alpaca.markets/v2/account"
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


def write_keychain_secret(account: str, service: str, token: str) -> None:
    security = _security_framework()
    account_bytes = account.encode()
    service_bytes = service.encode()
    token_bytes = token.encode()
    token_buffer = ctypes.create_string_buffer(token_bytes)
    item = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None, len(service_bytes), service_bytes,
        len(account_bytes), account_bytes, None, None, ctypes.byref(item))
    if status == 0:
        status = security.SecKeychainItemModifyAttributesAndData(
            item, None, len(token_bytes), ctypes.cast(token_buffer, ctypes.c_void_p))
    elif status == ERR_SEC_ITEM_NOT_FOUND:
        status = security.SecKeychainAddGenericPassword(
            None, len(service_bytes), service_bytes,
            len(account_bytes), account_bytes, len(token_bytes),
            ctypes.cast(token_buffer, ctypes.c_void_p), None)
    if status != 0:
        raise KeychainError(f"Keychain returned OSStatus {status}")


def normalize_key(value: str) -> str:
    key = (value or "").strip()
    if key.lower().startswith("apikey="):
        key = key.split("=", 1)[1].strip()
    if key.lower().startswith("api_key="):
        key = key.split("=", 1)[1].strip()
    if key.lower().startswith("authorization:"):
        key = key.split(":", 1)[1].strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


def validate_key_pair(api_key: str, api_secret: str, *, session=requests) -> tuple[bool, str]:
    try:
        response = session.get(
            VERIFY_URL,
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
            timeout=15,
        )
    except requests.RequestException:
        return False, "Could not reach Alpaca paper. Nothing was stored."
    if response.status_code == 200:
        return True, "Alpaca paper API credentials accepted."
    if response.status_code == 401:
        return False, "Alpaca rejected the paper API credentials (HTTP 401). Nothing was stored."
    if response.status_code == 403:
        return False, "Alpaca recognized the credentials, but paper API access is not authorized (HTTP 403). Nothing was stored."
    if response.status_code == 429:
        return False, "Alpaca rate-limited the validation request (HTTP 429). Nothing was stored."
    return False, f"Alpaca returned HTTP {response.status_code}. Nothing was stored."


def store_key_pair(api_key: str, api_secret: str) -> tuple[bool, str]:
    account = os.environ.get("USER") or getpass.getuser()
    try:
        write_keychain_secret(account, SERVICE_KEY, api_key)
        write_keychain_secret(account, SERVICE_SECRET, api_secret)

        key_readback = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", SERVICE_KEY, "-w"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        secret_readback = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", SERVICE_SECRET, "-w"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        if (
            key_readback.returncode != 0
            or secret_readback.returncode != 0
            or not hmac.compare_digest(normalize_key(key_readback.stdout), api_key)
            or not hmac.compare_digest(normalize_key(secret_readback.stdout), api_secret)
        ):
            return False, "Keychain verification failed; the validated Alpaca pair was not stored."
    except (OSError, KeychainError):
        return False, "macOS Keychain could not be opened."
    return True, "Alpaca paper credentials stored securely in macOS Keychain."


def main() -> int:
    print("Alpaca paper API setup for Scalpr")
    print("Paste the raw API key and secret below. Do not enter your Alpaca password.")
    api_key = normalize_key(getpass.getpass("API key (hidden): "))
    if not api_key:
        print("No API key entered. Nothing was stored.")
        return 1
    if any(ch.isspace() for ch in api_key):
        print("The API key contains spaces and does not look valid. Nothing was stored.")
        return 1

    api_secret = normalize_key(getpass.getpass("API secret (hidden): "))
    if not api_secret:
        print("No API secret entered. Nothing was stored.")
        return 1
    if any(ch.isspace() for ch in api_secret):
        print("The API secret contains spaces and does not look valid. Nothing was stored.")
        return 1

    valid, message = validate_key_pair(api_key, api_secret)
    print(message)
    if not valid:
        return 1

    stored, message = store_key_pair(api_key, api_secret)
    print(message)
    return 0 if stored else 1


if __name__ == "__main__":
    sys.exit(main())
