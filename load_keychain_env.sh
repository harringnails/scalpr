#!/bin/bash
# Load optional local provider credentials without storing them in this repo.
# This file is sourced by the interactive and headless restart scripts.

SCALPR_UW_KEYCHAIN_STATUS="missing"
SCALPR_IVOL_KEYCHAIN_STATUS="missing"
SCALPR_ALPACA_KEY_KEYCHAIN_STATUS="missing"
SCALPR_ALPACA_SECRET_KEYCHAIN_STATUS="missing"

SCALPR_UW_EXISTING_ENV="${UW_API_KEY:-}"
SCALPR_IVOL_EXISTING_ENV="${IVOLATILITY_API_KEY:-}"
SCALPR_ALPACA_KEY_EXISTING_ENV="${ALPACA_API_KEY:-}"
SCALPR_ALPACA_SECRET_EXISTING_ENV="${ALPACA_SECRET_KEY:-}"

# Prefer the validated Keychain item over a possibly stale inherited environment
# variable. Fall back to the environment only when Keychain is unavailable.
if command -v security >/dev/null 2>&1; then
  SCALPR_UW_ACCOUNT="${USER:-$(id -un 2>/dev/null)}"
  if [ -n "$SCALPR_UW_ACCOUNT" ]; then
    SCALPR_UW_TOKEN="$(security find-generic-password \
      -a "$SCALPR_UW_ACCOUNT" \
      -s "scalpr.unusualwhales.api" \
      -w 2>/dev/null || true)"
    if [ -n "$SCALPR_UW_TOKEN" ]; then
      export UW_API_KEY="$SCALPR_UW_TOKEN"
      SCALPR_UW_KEYCHAIN_STATUS="keychain"
    fi
    unset SCALPR_UW_TOKEN
  fi
  unset SCALPR_UW_ACCOUNT
fi

# IVolatility uses its own Keychain item and environment variable. It never
# shares or aliases the Unusual Whales credential.
if command -v security >/dev/null 2>&1; then
  SCALPR_IVOL_ACCOUNT="${USER:-$(id -un 2>/dev/null)}"
  if [ -n "$SCALPR_IVOL_ACCOUNT" ]; then
    SCALPR_IVOL_TOKEN="$(security find-generic-password \
      -a "$SCALPR_IVOL_ACCOUNT" \
      -s "scalpr.ivolatility.api" \
      -w 2>/dev/null || true)"
    if [ -n "$SCALPR_IVOL_TOKEN" ]; then
      export IVOLATILITY_API_KEY="$SCALPR_IVOL_TOKEN"
      SCALPR_IVOL_KEYCHAIN_STATUS="keychain"
    fi
    unset SCALPR_IVOL_TOKEN
  fi
  unset SCALPR_IVOL_ACCOUNT
fi

# Alpaca PAPER trading uses two Keychain items (key + secret). Paper only; these
# are never aliased to the UW or IVolatility credentials, and never logged.
if command -v security >/dev/null 2>&1; then
  SCALPR_ALPACA_ACCOUNT="${USER:-$(id -un 2>/dev/null)}"
  if [ -n "$SCALPR_ALPACA_ACCOUNT" ]; then
    SCALPR_ALPACA_KEY_TOKEN="$(security find-generic-password \
      -a "$SCALPR_ALPACA_ACCOUNT" \
      -s "scalpr.alpaca.paper.key" \
      -w 2>/dev/null || true)"
    if [ -n "$SCALPR_ALPACA_KEY_TOKEN" ]; then
      export ALPACA_API_KEY="$SCALPR_ALPACA_KEY_TOKEN"
      SCALPR_ALPACA_KEY_KEYCHAIN_STATUS="keychain"
    fi
    unset SCALPR_ALPACA_KEY_TOKEN
    SCALPR_ALPACA_SECRET_TOKEN="$(security find-generic-password \
      -a "$SCALPR_ALPACA_ACCOUNT" \
      -s "scalpr.alpaca.paper.secret" \
      -w 2>/dev/null || true)"
    if [ -n "$SCALPR_ALPACA_SECRET_TOKEN" ]; then
      export ALPACA_SECRET_KEY="$SCALPR_ALPACA_SECRET_TOKEN"
      SCALPR_ALPACA_SECRET_KEYCHAIN_STATUS="keychain"
    fi
    unset SCALPR_ALPACA_SECRET_TOKEN
  fi
  unset SCALPR_ALPACA_ACCOUNT
fi

if [ "$SCALPR_UW_KEYCHAIN_STATUS" = "missing" ] && [ -n "$SCALPR_UW_EXISTING_ENV" ]; then
  export UW_API_KEY="$SCALPR_UW_EXISTING_ENV"
  SCALPR_UW_KEYCHAIN_STATUS="environment"
fi

unset SCALPR_UW_EXISTING_ENV

if [ "$SCALPR_IVOL_KEYCHAIN_STATUS" = "missing" ] && [ -n "$SCALPR_IVOL_EXISTING_ENV" ]; then
  export IVOLATILITY_API_KEY="$SCALPR_IVOL_EXISTING_ENV"
  SCALPR_IVOL_KEYCHAIN_STATUS="environment"
fi

unset SCALPR_IVOL_EXISTING_ENV

if [ "$SCALPR_ALPACA_KEY_KEYCHAIN_STATUS" = "missing" ] && [ -n "$SCALPR_ALPACA_KEY_EXISTING_ENV" ]; then
  export ALPACA_API_KEY="$SCALPR_ALPACA_KEY_EXISTING_ENV"
  SCALPR_ALPACA_KEY_KEYCHAIN_STATUS="environment"
fi

unset SCALPR_ALPACA_KEY_EXISTING_ENV

if [ "$SCALPR_ALPACA_SECRET_KEYCHAIN_STATUS" = "missing" ] && [ -n "$SCALPR_ALPACA_SECRET_EXISTING_ENV" ]; then
  export ALPACA_SECRET_KEY="$SCALPR_ALPACA_SECRET_EXISTING_ENV"
  SCALPR_ALPACA_SECRET_KEYCHAIN_STATUS="environment"
fi

unset SCALPR_ALPACA_SECRET_EXISTING_ENV

# A launchd-managed session can carry credentials even when a fresh Terminal
# does not inherit them. Use that source only after Keychain and the process
# environment have both been exhausted; never print or persist the values.
if command -v launchctl >/dev/null 2>&1; then
  if [ "$SCALPR_ALPACA_KEY_KEYCHAIN_STATUS" = "missing" ]; then
    SCALPR_ALPACA_KEY_LAUNCHD_VALUE="$(launchctl getenv ALPACA_API_KEY 2>/dev/null || true)"
    if [ -n "$SCALPR_ALPACA_KEY_LAUNCHD_VALUE" ]; then
      export ALPACA_API_KEY="$SCALPR_ALPACA_KEY_LAUNCHD_VALUE"
      SCALPR_ALPACA_KEY_KEYCHAIN_STATUS="launchctl"
    fi
    unset SCALPR_ALPACA_KEY_LAUNCHD_VALUE
  fi

  if [ "$SCALPR_ALPACA_SECRET_KEYCHAIN_STATUS" = "missing" ]; then
    SCALPR_ALPACA_SECRET_LAUNCHD_VALUE="$(launchctl getenv ALPACA_SECRET_KEY 2>/dev/null || true)"
    if [ -n "$SCALPR_ALPACA_SECRET_LAUNCHD_VALUE" ]; then
      export ALPACA_SECRET_KEY="$SCALPR_ALPACA_SECRET_LAUNCHD_VALUE"
      SCALPR_ALPACA_SECRET_KEYCHAIN_STATUS="launchctl"
    fi
    unset SCALPR_ALPACA_SECRET_LAUNCHD_VALUE
  fi
fi
