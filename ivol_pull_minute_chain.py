"""Pull one contract's IVolatility intraday minute quotes -> CSV (IVOL env only).

Runs in the IVolatility VS Code environment (where IVOLATILITY_API_KEY is set).
Read-only: hits GET /equities/intraday/single-equity-option-rawiv and writes the
handoff CSV that ivol_fidelity_probe.py consumes:

    timestamp, option_symbol, bid, ask, bid_size, ask_size

No broker/order/Guard imports. The key is never placed in a URL or logged.

Example:
    python ivol_pull_minute_chain.py \
        --symbol SPY --exp-date 2026-08-11 --strike 560 --type CALL \
        --date 2026-08-11 --minute-type MINUTE_1 \
        --out ivol_SPY_560C_2026-08-11_m1.csv

IMPORTANT — confirm the timezone of the returned `timestamp` field before
trusting the comparison. Set --emit-tz-note to print a one-row sample so you can
verify whether it is ET or UTC, then pass that tz to the probe config as
`ivol_timestamp_tz`.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

import requests

BASE_URL = "https://restapi.ivolatility.com"
INTRADAY_PATH = "/equities/intraday/single-equity-option-rawiv"
ENV_KEY = "IVOLATILITY_API_KEY"

# Raw IVOL field -> handoff column.
FIELD_MAP = {
    "optionBidPrice": "bid",
    "optionAskPrice": "ask",
    "optionBidSize": "bid_size",
    "optionAskSize": "ask_size",
}


def fetch(symbol, exp_date, strike, opt_type, date, minute_type, timeout=20.0):
    api_key = (os.getenv(ENV_KEY) or "").strip()
    if not api_key:
        sys.exit(f"{ENV_KEY} is not set in this environment")
    params = {
        "apiKey": api_key,
        "symbol": symbol,
        "date": date,
        "expDate": exp_date,
        "strike": strike,
        "optType": opt_type,       # CALL / PUT (full word)
        "minuteType": minute_type,  # MINUTE_1 | MINUTE_5 | ...
    }
    try:
        resp = requests.get(BASE_URL + INTRADAY_PATH, params=params, timeout=timeout)
    except requests.RequestException:
        # Never surface the prepared URL (it carries the key).
        sys.exit("IVolatility request failed")
    if resp.status_code == 204:
        return []
    if resp.status_code != 200:
        sys.exit(f"IVolatility API HTTP {resp.status_code}")
    payload = resp.json()
    if not isinstance(payload, dict):
        sys.exit("unexpected IVolatility payload")
    return payload.get("data") or []


def to_rows(data, option_symbol):
    out = []
    for rec in data:
        row = {"timestamp": rec.get("timestamp"), "option_symbol":
               rec.get("optionSymbol") or option_symbol}
        for raw, col in FIELD_MAP.items():
            row[col] = rec.get(raw)
        out.append(row)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pull IVOL intraday minute chain -> CSV")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--exp-date", required=True)
    ap.add_argument("--strike", required=True, type=float)
    ap.add_argument("--type", required=True, choices=["CALL", "PUT"])
    ap.add_argument("--date", required=True)
    ap.add_argument("--minute-type", default="MINUTE_1")
    ap.add_argument("--option-symbol", default="", help="OSI symbol for the CSV")
    ap.add_argument("--out", required=True)
    ap.add_argument("--emit-tz-note", action="store_true",
                    help="print the first raw timestamp so you can confirm tz")
    args = ap.parse_args(argv)

    data = fetch(args.symbol, args.exp_date, args.strike, args.type,
                 args.date, args.minute_type)
    if not data:
        print("NO_DATA returned for this contract/date", file=sys.stderr)
    if args.emit_tz_note and data:
        print(f"[tz-check] first raw timestamp = {data[0].get('timestamp')!r} "
              f"-> confirm ET vs UTC before setting ivol_timestamp_tz",
              file=sys.stderr)

    rows = to_rows(data, args.option_symbol)
    cols = ["timestamp", "option_symbol", "bid", "ask", "bid_size", "ask_size"]
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"wrote {len(rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
