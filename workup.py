#!/usr/bin/env python3
"""
workup.py v3 — Unusual Whales pre-trade workup.

New in v3:
  - atm-chains call (requires expirations[]) for next_earnings_date, er_time,
    and the ATM aggressor split. stock/{t}/earnings is HISTORY only; the
    forward date lives here.
  - Tightest-spread table restricted to delta 0.15-0.70 and shows dollar
    spread. Percent-of-mid flatters deep ITM contracts ($928 mid at 1.28%
    is an $11.90 spread nobody can trade).
  - Volume floor + IV sanity band in tradeable(): implied_volatility is
    documented as "Last Transaction IV", so it is stale or zero on
    illiquid contracts, which made locally-computed greeks meaningless.

Usage:
    export UW_API_KEY="..."
    python3 workup.py GOOGL --dte 7 60
    python3 workup.py MU --dte 30 120 --json mu.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime

import requests

BASE = "https://api.unusualwhales.com/api"
TIMEOUT = 30
SLEEP = 0.25
PAGE_LIMIT = 500

OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


# ---------------------------------------------------------------- helpers

def parse_occ(sym: str):
    m = OCC.match(sym or "")
    if not m:
        return None
    tkr, ymd, cp, strike = m.groups()
    try:
        exp = datetime.strptime(ymd, "%y%m%d").date()
    except ValueError:
        return None
    return tkr, exp, cp, int(strike) / 1000.0


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def d(v):
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def greeks(spot, strike, dte_days, iv, cp, r=0.043):
    if not all([spot, strike, iv]) or dte_days <= 0 or iv <= 0:
        return {}
    T = dte_days / 365.0
    sq = iv * math.sqrt(T)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * T) / sq
    d2 = d1 - sq
    disc = math.exp(-r * T)

    if cp == "C":
        delta = ncdf(d1)
        theta = (-(spot * npdf(d1) * iv) / (2 * math.sqrt(T))
                 - r * strike * disc * ncdf(d2))
    else:
        delta = ncdf(d1) - 1.0
        theta = (-(spot * npdf(d1) * iv) / (2 * math.sqrt(T))
                 + r * strike * disc * ncdf(-d2))

    return {
        "delta": round(delta, 4),
        "gamma": round(npdf(d1) / (spot * sq), 6),
        "theta": round(theta / 365.0, 4),
        "vega": round(spot * npdf(d1) * math.sqrt(T) / 100.0, 4),
    }


# ---------------------------------------------------------------- client

class UW:
    def __init__(self, key):
        token = str(key or "").strip()
        if token.lower().startswith("authorization:"):
            token = token.split(":", 1)[1].strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            raise ValueError("Unusual Whales API token is empty")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Accept": "application/json"})

    def get(self, path, **params):
        p = {k: v for k, v in params.items() if v is not None}
        try:
            r = self.s.get(f"{BASE}/{path}", params=p or None, timeout=TIMEOUT)
        except requests.RequestException as e:
            return None, f"error: {e}"[:120]
        if r.status_code != 200:
            return None, f"http_{r.status_code}"
        try:
            body = r.json()
        except ValueError:
            return None, "non-JSON"
        return (body.get("data", body) if isinstance(body, dict) else body), "ok"


def fetch_all_contracts(uw, ticker, expiries, verbose=True):
    rows = []
    for exp in expiries:
        page, got = 0, 0
        while True:
            data, status = uw.get(
                f"stock/{ticker}/option-contracts",
                expiry=exp,
                exclude_zero_oi_chains="true",
                limit=PAGE_LIMIT,
                page=page,
            )
            if status != "ok" or not isinstance(data, list):
                if verbose:
                    print(f"  ! {exp} page {page}: {status}", file=sys.stderr)
                break
            rows.extend(data)
            got += len(data)
            if len(data) < PAGE_LIMIT:
                break
            page += 1
            time.sleep(SLEEP)
        if verbose:
            print(f"  {exp}: {got} contracts", file=sys.stderr)
        time.sleep(SLEEP)
    return rows


def fetch_atm(uw, ticker, expiry):
    """atm-chains requires expirations[]. Carries next_earnings_date."""
    data, status = uw.get(f"stock/{ticker}/atm-chains",
                          **{"expirations[]": expiry})
    if status != "ok" or not isinstance(data, list) or not data:
        return None, status
    return data, "ok"


def atm_summary(rows):
    """Earnings date plus aggregate ATM aggressor split."""
    if not rows:
        return {}
    out = {}
    for r in rows:
        if isinstance(r, dict) and r.get("next_earnings_date"):
            out["next_earnings_date"] = r["next_earnings_date"]
            out["er_time"] = r.get("er_time")
            out["stock_price"] = r.get("stock_price")
            out["sector"] = r.get("sector")
            break

    ask = sum(f(r.get("ask_side_volume")) or 0 for r in rows
              if isinstance(r, dict))
    bid = sum(f(r.get("bid_side_volume")) or 0 for r in rows
              if isinstance(r, dict))
    ml = sum(f(r.get("multileg_volume")) or 0 for r in rows
             if isinstance(r, dict))
    vol = sum(f(r.get("volume")) or 0 for r in rows if isinstance(r, dict))
    prem = sum(f(r.get("premium")) or 0 for r in rows if isinstance(r, dict))

    out.update({
        "atm_volume": int(vol),
        "atm_ask_pct": round(ask / vol * 100, 1) if vol else None,
        "atm_bid_pct": round(bid / vol * 100, 1) if vol else None,
        "atm_multileg_pct": round(ml / vol * 100, 1) if vol else None,
        "atm_premium": prem,
    })
    return out


# ---------------------------------------------------------------- analysis

def build_contracts(rows, spot, today, dte_lo, dte_hi, rate):
    out = []
    for c in rows:
        if not isinstance(c, dict):
            continue
        parsed = parse_occ(c.get("option_symbol", ""))
        if not parsed:
            continue
        _, exp, cp, strike = parsed
        dte = (exp - today).days
        if dte < dte_lo or dte > dte_hi:
            continue

        bid, ask = f(c.get("nbbo_bid")), f(c.get("nbbo_ask"))
        if not bid or not ask or ask < bid or bid <= 0:
            continue
        mid = (bid + ask) / 2
        iv = f(c.get("implied_volatility"))

        oi = f(c.get("open_interest")) or 0
        prev = f(c.get("prev_oi")) or 0
        vol = f(c.get("volume")) or 0
        ml = f(c.get("multi_leg_volume")) or 0
        av, bv = f(c.get("ask_volume")) or 0, f(c.get("bid_volume")) or 0

        rec = {
            "symbol": c.get("option_symbol"),
            "expiry": exp.isoformat(),
            "dte": dte,
            "type": cp,
            "strike": strike,
            "moneyness": round(strike / spot, 4) if spot else None,
            "bid": bid, "ask": ask, "mid": round(mid, 3),
            "spread_abs": round(ask - bid, 2),
            "spread_pct": round((ask - bid) / mid * 100, 2),
            "iv": round(iv, 4) if iv else None,
            "oi": int(oi), "prev_oi": int(prev), "oi_chg": int(oi - prev),
            "volume": int(vol),
            "single_leg_vol": int(vol - ml),
            "multileg_pct": round(ml / vol * 100, 1) if vol else None,
            "net_aggressor": int(av - bv),
            "ask_pct": round(av / vol * 100, 1) if vol else None,
            "sweep_vol": int(f(c.get("sweep_volume")) or 0),
            "premium": f(c.get("total_premium")),
            "vol_oi": round(vol / oi, 2) if oi else None,
        }
        rec.update(greeks(spot, strike, dte, iv, cp, rate))
        out.append(rec)
    return out


def tradeable(c, max_spread=10.0, min_oi=100, min_vol=10):
    """IV band guards against stale 'Last Transaction IV' on dead contracts."""
    iv = c.get("iv") or 0
    return (c["spread_pct"] <= max_spread
            and c["oi"] >= min_oi
            and c["volume"] >= min_vol
            and 0.01 < iv < 3.0)


def nearest_max_pain(mp, today):
    if not isinstance(mp, list):
        return None
    fut = []
    for row in mp:
        if not isinstance(row, dict):
            continue
        e = d(row.get("expiry"))
        if e and e >= today:
            fut.append((e, row))
    if not fut:
        return None
    fut.sort(key=lambda x: x[0])
    return fut[0][1]


# ---------------------------------------------------------------- report

def report(t, spot, ctx, atm, contracts, today):
    L = [f"\n{'='*80}",
         f" {t}   spot {spot}   {time.strftime('%Y-%m-%d %H:%M')}",
         "=" * 80]

    if atm.get("next_earnings_date"):
        ed = d(atm["next_earnings_date"])
        days = (ed - today).days if ed else None
        L.append(f"\n** NEXT EARNINGS: {atm['next_earnings_date']}"
                 f" ({atm.get('er_time')})"
                 + (f"  — {days} days out **" if days is not None else " **"))
    else:
        L.append("\n** earnings date unavailable — verify independently **")

    iv = ctx.get("iv-rank")
    if isinstance(iv, list) and iv:
        last = iv[-1]
        L.append(f"IV rank (1y): {last.get('iv_rank_1y')}   "
                 f"IV {last.get('volatility')}   as of {last.get('date')}")

    mp = nearest_max_pain(ctx.get("max-pain"), today)
    if mp:
        L.append(f"Max pain ({mp.get('expiry')}): {mp.get('max_pain')}  "
                 f"[{mp.get('next_lower_strike')} / "
                 f"{mp.get('next_upper_strike')}]")

    if atm.get("atm_volume"):
        L.append(f"ATM chain: vol {atm['atm_volume']:,}  "
                 f"ask {atm.get('atm_ask_pct')}%  "
                 f"bid {atm.get('atm_bid_pct')}%  "
                 f"multileg {atm.get('atm_multileg_pct')}%")

    exp = ctx.get("expiry-breakdown")
    if isinstance(exp, list) and exp:
        L.append("\nExpiry       chains       OI      volume")
        for e in exp[:8]:
            L.append(f"  {e.get('expires')}  {e.get('chains'):>6} "
                     f"{e.get('open_interest'):>9} {e.get('volume'):>10}")

    liq = [c for c in contracts if tradeable(c)]
    L.append(f"\n{len(contracts)} contracts in DTE window, "
             f"{len(liq)} tradeable (spread<=10%, OI>=100, vol>=10)")

    if not liq:
        return "\n".join(L)

    spreads = sorted(c["spread_pct"] for c in liq)
    L.append(f"Median spread among tradeable: "
             f"{spreads[len(spreads)//2]}% of mid")

    band = [c for c in liq if 0.15 <= abs(c.get("delta") or 0) <= 0.70]
    if band:
        L.append("\n-- Tightest spreads, delta 0.15-0.70 --")
        L.append(f"{'expiry':<12}{'strike':>9} {'T':<2}{'dte':>5}{'spr%':>7}"
                 f"{'$spr':>7}{'mid':>9}{'iv':>7}{'delta':>7}{'theta':>8}"
                 f"{'OI':>8}{'vol':>8}")
        for c in sorted(band, key=lambda x: x["spread_pct"])[:12]:
            L.append(f"{c['expiry']:<12}{c['strike']:>9.1f} {c['type']:<2}"
                     f"{c['dte']:>5}{c['spread_pct']:>7}{c['spread_abs']:>7}"
                     f"{c['mid']:>9}{(c['iv'] or 0):>7.3f}"
                     f"{(c.get('delta') or 0):>7.3f}"
                     f"{(c.get('theta') or 0):>8.3f}{c['oi']:>8}"
                     f"{c['volume']:>8}")

    opening = [c for c in liq
               if c["oi_chg"] > 0 and c["volume"] > 0
               and (c["multileg_pct"] or 0) < 25]
    opening.sort(key=lambda x: x["oi_chg"], reverse=True)
    if opening:
        L.append("\n-- Largest OI increases, single-leg only (ml% < 25) --")
        L.append(f"{'expiry':<12}{'strike':>9} {'T':<2}{'oi_chg':>8}"
                 f"{'ask%':>7}{'ml%':>7}{'vol/oi':>8}{'delta':>7}"
                 f"{'premium':>14}")
        for c in opening[:14]:
            L.append(f"{c['expiry']:<12}{c['strike']:>9.1f} {c['type']:<2}"
                     f"{c['oi_chg']:>8}{(c['ask_pct'] or 0):>7.1f}"
                     f"{(c['multileg_pct'] or 0):>7.1f}"
                     f"{(c['vol_oi'] or 0):>8.2f}{(c.get('delta') or 0):>7.3f}"
                     f"{(c['premium'] or 0):>14,.0f}")

    return "\n".join(L)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--dte", nargs=2, type=int, default=[7, 90],
                    metavar=("LO", "HI"))
    ap.add_argument("--rate", type=float, default=0.043)
    ap.add_argument("--json", metavar="PATH")
    a = ap.parse_args()

    key = os.environ.get("UW_API_KEY")
    if not key:
        print("set UW_API_KEY first", file=sys.stderr)
        return 1
    uw = UW(key)
    t = a.ticker.upper()
    today = date.today()

    state, st = uw.get(f"stock/{t}/stock-state")
    spot = None
    if isinstance(state, dict):
        spot = f(state.get("close") or state.get("last") or state.get("price"))
    elif isinstance(state, list) and state:
        spot = f(state[0].get("close"))
    if not spot:
        print(f"could not resolve spot ({st}); raw: {str(state)[:250]}",
              file=sys.stderr)
        return 1

    ctx = {}
    for name, path in [
        ("iv-rank", f"stock/{t}/iv-rank"),
        ("vol-stats", f"stock/{t}/volatility/stats"),
        ("realized", f"stock/{t}/volatility/realized"),
        ("term-structure", f"stock/{t}/volatility/term-structure"),
        ("variance-risk-premium", f"stock/{t}/volatility/variance-risk-premium"),
        ("skew", f"stock/{t}/historical-risk-reversal-skew"),
        ("max-pain", f"stock/{t}/max-pain"),
        ("gex-levels", f"stock/{t}/gex-levels"),
        ("oi-change", f"stock/{t}/oi-change"),
        ("expiry-breakdown", f"stock/{t}/expiry-breakdown"),
        ("flow-alerts", f"stock/{t}/flow-alerts"),
        ("darkpool", f"darkpool/{t}"),
        ("earnings", f"stock/{t}/earnings"),
        ("short-interest", f"shorts/{t}/interest-float/v2"),
    ]:
        data, status = uw.get(path)
        ctx[name] = data
        if status != "ok":
            print(f"  ! {name}: {status}", file=sys.stderr)
        time.sleep(SLEEP)

    expiries = []
    for e in (ctx.get("expiry-breakdown") or []):
        if not isinstance(e, dict):
            continue
        ed = d(e.get("expires"))
        if ed and a.dte[0] <= (ed - today).days <= a.dte[1]:
            expiries.append(ed.isoformat())

    if not expiries:
        print(f"no expiries in DTE window {a.dte[0]}-{a.dte[1]}",
              file=sys.stderr)
        return 1

    # atm-chains on nearest expiry in window: carries next_earnings_date
    atm_rows, atm_status = fetch_atm(uw, t, expiries[0])
    if atm_status != "ok":
        print(f"  ! atm-chains: {atm_status}", file=sys.stderr)
    atm = atm_summary(atm_rows or [])
    ctx["atm-chains"] = atm_rows

    print(f"\nfetching {len(expiries)} expiries", file=sys.stderr)
    rows = fetch_all_contracts(uw, t, expiries)
    print(f"  total {len(rows)} raw contracts", file=sys.stderr)

    contracts = build_contracts(rows, spot, today, a.dte[0], a.dte[1], a.rate)
    print(report(t, spot, ctx, atm, contracts, today))

    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"ticker": t, "spot": spot, "as_of": today.isoformat(),
                       "atm": atm, "context": ctx, "contracts": contracts},
                      fh, indent=2, default=str)
        print(f"\nwrote {a.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
