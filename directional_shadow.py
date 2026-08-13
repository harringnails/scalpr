"""Automated directional SHADOW proposals for Scalpr.

This module turns the existing exploratory entry read into an auditable
CALL/PUT/NO_TRADE proposal and selects one cached Workup contract.  It cannot
submit, replace, or cancel an order: it imports no broker/trading client and
receives only plain dictionaries.

The proposal layer is deliberately non-promotable.  It is machinery for a
forward shadow cohort, not a claim that the entry policy is profitable.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path


VERSION = "directional-shadow-v0"
FORMAL_COHORT_ELIGIBLE = False
DEFAULT_LOG = Path("directional_shadow_v0.jsonl")

DEFAULT_LIMITS = {
    "symbol_scope": ["SPY"],
    "max_contracts_per_entry": 1,
    "max_entries_per_day": 3,
    "max_premium_per_entry_usd": 500.0,
    "max_daily_premium_usd": 1500.0,
    "target_abs_delta": 0.45,
    "climb_add_contracts": 1,
    "climb_max_adds": 2,
    "automatic_exit_requires_separate_authorization": True,
}


def _right(value):
    value = str(value or "").strip().upper()
    return "CALL" if value in ("C", "CALL") else "PUT" if value in ("P", "PUT") else None


def _pick_contract(contracts, direction, limits):
    """Choose one executable, clean, affordable contract deterministically."""
    wanted = "CALL" if direction == "CALL" else "PUT"
    target_delta = float(limits["target_abs_delta"])
    cap = float(limits["max_premium_per_entry_usd"])
    eligible = []
    for c in contracts or []:
        ask = float(c.get("ask") or 0)
        delta = c.get("delta")
        if (_right(c.get("type")) != wanted or not c.get("clean") or ask <= 0
                or delta is None or ask * 100 > cap):
            continue
        eligible.append(c)
    if not eligible:
        return None
    eligible.sort(key=lambda c: (
        abs(abs(float(c["delta"])) - target_delta),
        float(c.get("spread_pct") or 999),
        -int(c.get("volume") or 0),
        -int(c.get("oi") or 0),
        str(c.get("symbol") or ""),
    ))
    c = eligible[0]
    ask = float(c["ask"])
    return {
        "symbol": c["symbol"],
        "direction": wanted,
        "expiry": c.get("expiry"),
        "dte": c.get("dte"),
        "strike": c.get("strike"),
        "bid": c.get("bid"),
        "ask": ask,
        "spread_pct": c.get("spread_pct"),
        "delta": c.get("delta"),
        "oi": c.get("oi"),
        "volume": c.get("volume"),
        "proposed_quantity": int(limits["max_contracts_per_entry"]),
        "estimated_entry_cost_usd": round(
            ask * 100 * int(limits["max_contracts_per_entry"]), 2),
        "selection_rule": "clean_affordable_nearest_abs_delta_then_spread_liquidity",
    }


def build_proposal(entry_read, contracts, *, limits=None, now=None):
    """Pure mapping from frozen evidence to CALL/PUT/NO_TRADE."""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    symbol = str(entry_read.get("symbol") or "").upper()
    decision = str(entry_read.get("decision") or "NO_TRADE").upper()
    direction = ("CALL" if decision == "LONG_CANDIDATE" else
                 "PUT" if decision == "SHORT_CANDIDATE" else "NO_TRADE")
    reasons = []
    if symbol not in limits["symbol_scope"]:
        direction = "NO_TRADE"; reasons.append("SYMBOL_OUT_OF_SCOPE")
    if direction == "NO_TRADE":
        reasons.append(str(entry_read.get("reason") or decision))
    contract = None
    if direction != "NO_TRADE":
        contract = _pick_contract(contracts, direction, limits)
        if contract is None:
            direction = "NO_TRADE"; reasons.append("NO_CLEAN_AFFORDABLE_CONTRACT")
    stamp = now or datetime.now(timezone.utc)
    stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    minute = stamp.astimezone(timezone.utc).replace(second=0, microsecond=0)
    proposal_id = f"{VERSION}:{symbol}:{minute.isoformat()}"
    return {
        "version": VERSION,
        "proposal_id": proposal_id,
        "as_of": stamp.astimezone(timezone.utc).isoformat(),
        "symbol": symbol,
        "decision": direction,
        "source_policy_version": entry_read.get("policy_version"),
        "source_policy_decision": decision,
        "source_policy_reason": entry_read.get("reason"),
        "source_data_confidence": entry_read.get("data_confidence"),
        "source_missing_inputs": list(entry_read.get("missing_inputs") or []),
        "blocking_reasons": reasons,
        "proposed_contract": contract,
        "limits": limits,
        "shadow_only": True,
        "broker_orders_reachable": False,
        "formal_cohort_eligible": FORMAL_COHORT_ELIGIBLE,
        "note": ("Automated directional shadow proposal only. No broker order. "
                 "CALL/PUT candidates come from the exploratory entry policy; "
                 "NO_TRADE is the default."),
    }


def append_proposal(proposal, path=DEFAULT_LOG):
    """Append at most one immutable proposal per symbol/minute."""
    path = Path(path)
    pid = proposal["proposal_id"]
    if path.exists():
        with path.open() as f:
            for line in f:
                if f'"proposal_id":"{pid}"' in line.replace(" ", ""):
                    return False
    body = (json.dumps(proposal, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def _rows(path=DEFAULT_LOG, symbol=None):
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if symbol and row.get("symbol") != str(symbol).upper():
                continue
            out.append(row)
    return out


def status(symbol="SPY", path=DEFAULT_LOG):
    rows = _rows(path, symbol)
    counts = Counter(r.get("decision") for r in rows)
    return {
        "enabled": True,
        "version": VERSION,
        "shadow_only": True,
        "broker_orders_reachable": False,
        "formal_cohort_eligible": FORMAL_COHORT_ELIGIBLE,
        "limits": DEFAULT_LIMITS,
        "samples": len(rows),
        "counts": {k: counts.get(k, 0) for k in ("CALL", "PUT", "NO_TRADE")},
        "latest": rows[-1] if rows else None,
    }
