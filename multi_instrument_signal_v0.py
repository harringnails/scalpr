#!/usr/bin/env python3
"""Isolated four-ETF shadow capture, episode evaluation, and reporting."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import subprocess
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable

import requests

import signal_episode_common_v0 as common


STUDY_ID = "multi_instrument_signal_v0"
SYMBOLS = ("SPY", "QQQ", "IWM", "DIA")
ENDPOINTS = ("gex", "levels", "zero-dte")
BASE_URL = "https://lab.flashalpha.com/v1"
KEYCHAIN_SERVICE = "scalpr.flashalpha.api"
LOCK_DATE = date(2026, 9, 4)
FLASH_DAILY_LIMIT = 2500
FLASH_POLLS = 79
FLASH_CALLS_PER_SYMBOL = 3
EXISTING_SPY_DAILY_CALLS = 238
A_BPS = 2.5
S_BPS = 3.0
R_SECONDS = 180
W_SECONDS = 900
G_SECONDS = 120
V_MINUTES = 2
TARGET_N = 150
MAX_FLASH_AGE_SECONDS = 300.0
DEFAULT_PREREG = Path("PREREG_multi_instrument_signal_v0.md")
DEFAULT_MARKET_LEDGER = Path("multi_instrument_market_v0.jsonl")
DEFAULT_FLASH_LEDGER = Path("multi_instrument_flashalpha_v0.jsonl")
DEFAULT_STUDY_LEDGER = Path("multi_instrument_signal_v0.jsonl")
DEFAULT_REPORT = Path("multi_instrument_signal_report_v0.json")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def rate_budget() -> dict[str, int | bool]:
    multi = FLASH_POLLS * len(SYMBOLS) * FLASH_CALLS_PER_SYMBOL
    total = multi + EXISTING_SPY_DAILY_CALLS
    return {"fits": total <= FLASH_DAILY_LIMIT, "limit": FLASH_DAILY_LIMIT,
            "multi_calls": multi, "existing_calls": EXISTING_SPY_DAILY_CALLS,
            "total_calls": total, "headroom": FLASH_DAILY_LIMIT - total}


def bps_points(spot: float, basis_points: float) -> float:
    return spot * basis_points / 10000.0


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def keychain_key() -> str:
    result = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                            capture_output=True, text=True, check=False)
    key = result.stdout.strip()
    if result.returncode or not key:
        raise RuntimeError("FlashAlpha key is unavailable in Keychain")
    return key


def _ts(value: Any) -> datetime | None:
    return common.parse_timestamp(value)


def _number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, dict):
            value = value.get("price")
        try:
            result = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(result):
            return result
    return None


def _deep(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def provider_timestamp(payload: Any) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    for value in (payload.get("as_of"), payload.get("timestamp"), payload.get("updated_at"),
                  _deep(payload, "meta", "as_of"), _deep(payload, "data", "as_of")):
        parsed = _ts(value)
        if parsed:
            return parsed
    return None


def normalized_structure(payloads: dict[str, Any]) -> dict[str, Any]:
    gex, levels, zero = (payloads.get(name, {}) for name in ENDPOINTS)
    net = _number(gex.get("net_gex"), _deep(gex, "summary", "net_gex"),
                  zero.get("net_gex"), _deep(zero, "summary", "net_gex"))
    regime = str(zero.get("gamma_regime") or _deep(zero, "regime", "label") or
                 gex.get("net_gex_label") or gex.get("regime") or "").lower()
    if regime not in {"positive", "negative"} and net is not None:
        regime = "positive" if net > 0 else "negative" if net < 0 else "unknown"
    return {
        "call_wall": _number(levels.get("call_wall"), _deep(levels, "levels", "call_wall"),
                             zero.get("call_wall"), _deep(zero, "levels", "call_wall")),
        "put_wall": _number(levels.get("put_wall"), _deep(levels, "levels", "put_wall"),
                            zero.get("put_wall"), _deep(zero, "levels", "put_wall")),
        "gamma_flip": _number(levels.get("gamma_flip"), _deep(levels, "levels", "gamma_flip"),
                              zero.get("gamma_flip"), _deep(zero, "levels", "gamma_flip")),
        "spot": _number(levels.get("spot"), levels.get("underlying_price"),
                        gex.get("spot"), gex.get("underlying_price"),
                        zero.get("spot"), zero.get("underlying_price"),
                        _deep(levels, "underlying", "price")),
        "gamma_regime": regime or "unknown", "net_gex": net,
    }


def flash_poll_once(*, output: Path, api_key: str, requester: Callable[..., Any] = requests.get,
                    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict[str, Any]:
    if not rate_budget()["fits"]:
        raise RuntimeError("FlashAlpha daily budget gate failed")
    attempted = 0
    for symbol in SYMBOLS:
        payloads, endpoint_meta, rate_limited = {}, {}, False
        for endpoint in ENDPOINTS:
            requested = now()
            response = requester(f"{BASE_URL}/exposure/{endpoint}/{symbol}",
                                 headers={"X-Api-Key": api_key}, timeout=20)
            responded = now()
            attempted += 1
            payload = response.json() if response.status_code == 200 else None
            endpoint_meta[endpoint] = {
                "http_status": response.status_code, "provider_ts": (
                    provider_timestamp(payload).isoformat() if provider_timestamp(payload) else None),
                "request_ts": requested.isoformat(), "response_ts": responded.isoformat(),
                "rate_limit": {"limit": response.headers.get("X-RateLimit-Limit"),
                               "remaining": response.headers.get("X-RateLimit-Remaining")},
                "status": "AVAILABLE" if response.status_code == 200 else
                          "RATE_LIMITED" if response.status_code == 429 else "UNAVAILABLE",
            }
            if payload is not None:
                payloads[endpoint] = payload
            if response.status_code == 429:
                rate_limited = True
                break
        observed = now()
        states = [meta["status"] for meta in endpoint_meta.values()]
        provider_times = [_ts(meta["provider_ts"]) for meta in endpoint_meta.values()]
        provider_times = [stamp for stamp in provider_times if stamp]
        age = max(((observed - stamp).total_seconds() for stamp in provider_times), default=None)
        values = normalized_structure(payloads)
        row = {"record_type": "MULTI_INSTRUMENT_STRUCTURE", "study_id": STUDY_ID,
               "symbol": symbol, "observed_at_utc": observed.isoformat(), "values": values,
               "effective_ts_utc": max(provider_times).isoformat() if provider_times else None,
               "endpoint_provenance": endpoint_meta, "raw_payloads": payloads,
               "freshness": {"age_seconds": age, "status": "FRESH" if len(states) == 3 and
                              all(state == "AVAILABLE" for state in states) and age is not None and
                              age <= MAX_FLASH_AGE_SECONDS else "STALE_OR_UNAVAILABLE"},
               "is_inferential": False, "execution_authority": False}
        row["record_hash"] = canonical_hash(row)
        append_jsonl(output, row)
        if rate_limited:
            return {"attempted": attempted, "rate_limited": True}
    return {"attempted": attempted, "rate_limited": False}


def alpaca_poll_once(*, output: Path, api_key: str, api_secret: str,
                     requester: Callable[..., Any] = requests.get,
                     now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> int:
    observed = now()
    response = requester("https://data.alpaca.markets/v2/stocks/quotes/latest",
                         params={"symbols": ",".join(SYMBOLS), "feed": "sip"},
                         headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
                         timeout=20)
    response.raise_for_status()
    quotes = response.json().get("quotes", {})
    written = 0
    for symbol in SYMBOLS:
        quote = quotes.get(symbol) or {}
        bid, ask, stamp = _number(quote.get("bp")), _number(quote.get("ap")), _ts(quote.get("t"))
        clean = bid is not None and ask is not None and bid > 0 and ask > 0 and bid <= ask and stamp is not None
        age = (observed - stamp).total_seconds() if stamp else None
        row = {"record_type": "MULTI_INSTRUMENT_SIP_QUOTE", "study_id": STUDY_ID,
               "symbol": symbol, "provider_ts": stamp.isoformat() if stamp else None,
               "received_at_utc": observed.isoformat(), "bid": bid, "ask": ask,
               "mid": (bid + ask) / 2 if clean else None, "clean": clean,
               "age_seconds": age, "source": "Alpaca:SIP:latest_stock_quote",
               "is_inferential": False, "execution_authority": False}
        row["record_hash"] = canonical_hash(row)
        append_jsonl(output, row); written += 1
    return written


def poll_loop(function: Callable[[], Any], *, polls: int, interval: int,
              sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    completed = 0
    for index in range(polls):
        result = function(); completed += 1
        if isinstance(result, dict) and result.get("rate_limited"):
            return {"polls_completed": completed, "status": "RATE_LIMITED"}
        if index + 1 < polls:
            sleep(interval)
    return {"polls_completed": completed, "status": "COMPLETE"}


def market_quotes(rows: list[dict[str, Any]], symbol: str, session_date: str) -> list[dict[str, Any]]:
    opened, closed = common.session_bounds(session_date)
    lower = opened - timedelta(hours=5, minutes=30)
    result = []
    for row in rows:
        stamp = _ts(row.get("provider_ts"))
        if row.get("symbol") != symbol or not row.get("clean") or stamp is None or not lower <= stamp < closed:
            continue
        result.append({"bid": row["bid"], "ask": row["ask"], "mid": row["mid"],
                       "provider_ts": stamp, "received_at": _ts(row.get("received_at_utc")),
                       "receipt_age_seconds": row.get("age_seconds"), "source": row.get("source")})
    return sorted(result, key=lambda row: row["provider_ts"])


def structures(rows: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if row.get("symbol") != symbol:
            continue
        stamp = _ts(row.get("effective_ts_utc")) or _ts(row.get("observed_at_utc"))
        if stamp:
            result.append({**row, "effective_ts": stamp})
    return sorted(result, key=lambda row: row["effective_ts"])


def all_symbol_quotes(rows: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        stamp = _ts(row.get("provider_ts"))
        if row.get("symbol") == symbol and row.get("clean") and stamp:
            result.append({"bid": row["bid"], "ask": row["ask"], "mid": row["mid"],
                           "provider_ts": stamp, "received_at": _ts(row.get("received_at_utc")),
                           "receipt_age_seconds": row.get("age_seconds"), "source": row.get("source")})
    return sorted(result, key=lambda row: row["provider_ts"])


def align_structures(rows: list[dict[str, Any]], quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aligned = []
    for source in rows:
        row = dict(source); quote = common.latest_at_or_before(quotes, row["effective_ts"])
        row["alpaca_alignment"] = common.quote_provenance(quote, row["effective_ts"])
        flash_spot = _number(_deep(row, "values", "spot"))
        row["alpaca_alignment"]["flashalpha_spot"] = flash_spot
        row["alpaca_alignment"]["spot_delta_bps"] = (
            round((quote["mid"] / flash_spot - 1) * 10000, 6) if quote and flash_spot else None)
        if quote is None:
            row["freshness"] = {**row.get("freshness", {}), "status": "STALE_OR_UNAVAILABLE"}
        aligned.append(row)
    return aligned


def _base(prereg: Path, session_date: str, symbol: str, arm: str, cohort: str) -> dict[str, Any]:
    return {"study_id": STUDY_ID, "record_type": "MULTI_INSTRUMENT_EPISODE",
            "session_date": session_date, "instrument": symbol, "arm": arm, "cohort": cohort,
            "frozen_prereg_sha256": common.file_sha256(prereg), "counts_toward_n": False,
            "episode_status": "EXCLUDED", "is_inferential": False,
            "execution_authority": False, "verdict_status": "UNDERPOWERED", "provenance": {}}


def _finish(row: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    row["exclusion_reason"] = reason
    row["record_hash"] = canonical_hash(row)
    return row


def _fresh_structure(row: dict[str, Any]) -> bool:
    return _deep(row, "freshness", "status") == "FRESH"


def _outcome(quotes: list[dict[str, Any]], t0: datetime) -> dict[str, Any]:
    return common.label_outcome(quotes, t0)


def evaluate_d1(session_date: str, symbol: str, quotes: list[dict[str, Any]],
                flash: list[dict[str, Any]], prereg: Path) -> dict[str, Any]:
    opened, closed = common.session_bounds(session_date)
    prior = [row for row in flash if row["effective_ts"].astimezone(common.ET).date() < opened.astimezone(common.ET).date() and _fresh_structure(row)]
    cohort = "UNROUTED"
    record = _base(prereg, session_date, symbol, "D1", cohort)
    if date.fromisoformat(session_date) <= LOCK_DATE:
        return _finish(record, "PRELOCK_IN_SAMPLE")
    if not prior:
        return _finish(record, "MISSING_PRIOR_CLOSE_STRUCTURE")
    source = prior[-1]; values = source.get("values", {}); flip = _number(values.get("gamma_flip")); spot = _number(values.get("spot"))
    if values.get("gamma_regime") != "negative" or flip is None or spot is None or spot >= flip:
        return _finish(record, "PRIOR_NEGATIVE_GAMMA_GATE_NOT_MET")
    open_quote = common.earliest_at_or_after(quotes, opened)
    if not open_quote:
        return _finish(record, "NO_FRESH_OPEN_QUOTE")
    pre = next(((i, q) for i, q in enumerate(quotes) if q["provider_ts"] < opened and q["mid"] >= flip), None)
    if open_quote["mid"] >= flip:
        if pre is None: return _finish(record, "NO_OBSERVED_PREMARKET_RECLAIM")
        cohort, start = "H1a", quotes.index(open_quote)
    else:
        crossing = next(((i, q) for i, q in enumerate(quotes) if opened <= q["provider_ts"] < closed and q["mid"] >= flip), None)
        if crossing is None: return _finish(record, "NO_RTH_RECLAIM")
        cohort, start = "H1b", crossing[0]
    record["cohort"] = cohort
    target = quotes[start]["provider_ts"] + timedelta(seconds=W_SECONDS)
    end = common.earliest_at_or_after(quotes, target)
    if end is None: return _finish(record, "MISSING_ACCEPTANCE_ENDPOINT")
    path = [q for q in quotes[start:] if q["provider_ts"] <= end["provider_ts"]]
    floor = flip - bps_points(flip, A_BPS)
    if common.gap_exceeds(path) or any(q["mid"] < floor for q in path):
        return _finish(record, "ACCEPTANCE_PATH_FAILED")
    below = sum((b["provider_ts"] - a["provider_ts"]).total_seconds() for a, b in zip(path, path[1:]) if a["mid"] < flip)
    if below > G_SECONDS: return _finish(record, "BACK_BELOW_GRACE_EXCEEDED")
    outcome = _outcome(quotes, end["provider_ts"])
    record.update({"anchor_t0_utc": end["provider_ts"].isoformat(), "frozen_flip": flip,
                   "thresholds": {"A_bps": A_BPS, "W_seconds": W_SECONDS, "G_seconds": G_SECONDS},
                   "outcome": outcome, "provenance": {"prior_structure_hash": source.get("record_hash"),
                   "prior_provider": source.get("endpoint_provenance"),
                   "prior_alpaca_alignment": source.get("alpaca_alignment"), "quote_source": path[0]["source"]}})
    if outcome["a2_outcome_status"] != "A2-AVAILABLE": return _finish(record, "A2_UNAVAILABLE_MISSING_POINT")
    record.update({"counts_toward_n": True, "episode_status": "A2-AVAILABLE"})
    return _finish(record)


def minute_closes(quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = {}
    for quote in quotes: buckets[int(quote["provider_ts"].timestamp()) // 60] = dict(quote)
    rows = [buckets[key] for key in sorted(buckets)]; running = 0.0
    for index, row in enumerate(rows, 1): running += row["mid"]; row["proxy_vwap"] = running / index
    return rows


def evaluate_d2(session_date: str, symbol: str, quotes: list[dict[str, Any]],
                flash: list[dict[str, Any]], prereg: Path) -> dict[str, Any]:
    record = _base(prereg, session_date, symbol, "D2", "SINGLE")
    if date.fromisoformat(session_date) <= LOCK_DATE: return _finish(record, "PRELOCK_IN_SAMPLE")
    opened, closed = common.session_bounds(session_date)
    q = [row for row in quotes if opened <= row["provider_ts"] < closed]
    f = [row for row in flash if opened <= row["effective_ts"] < closed and _fresh_structure(row)]
    if not q: return _finish(record, "MISSING_RTH_QUOTES")
    if not f: return _finish(record, "MISSING_OR_STALE_FLASHALPHA_SESSION")
    closes = minute_closes(q); low = q[0]["mid"]
    for index, sweep in enumerate(q[1:], 1):
        threshold = bps_points(low, S_BPS)
        if sweep["mid"] > low - threshold: low = min(low, sweep["mid"]); continue
        reclaim = next((x for x in q[index + 1:] if 0 <= (x["provider_ts"] - sweep["provider_ts"]).total_seconds() <= R_SECONDS and x["mid"] >= low), None)
        if not reclaim: continue
        positive = 0; acceptance_index = None
        for i in range(1, len(closes)):
            if closes[i]["provider_ts"] <= reclaim["provider_ts"]: continue
            positive = positive + 1 if closes[i]["mid"] > closes[i-1]["mid"] else 0
            if positive >= 3: acceptance_index = i; break
        if acceptance_index is None: continue
        proxy = next((closes[i] for i in range(max(acceptance_index, V_MINUTES), len(closes))
                      if closes[i]["mid"] >= closes[i]["proxy_vwap"] and
                      closes[i]["proxy_vwap"] > closes[i-V_MINUTES]["proxy_vwap"]), None)
        if proxy is None: continue
        cross = None
        for prior_q, current in zip(q, q[1:]):
            if current["provider_ts"] < proxy["provider_ts"]: continue
            source = next((row for row in reversed(f) if row["effective_ts"] <= current["provider_ts"]), None)
            wall = _number(_deep(source, "values", "call_wall")) if source else None
            if wall and prior_q["mid"] < wall <= current["mid"]: cross = (current, wall, source); break
        if not cross: continue
        migration = next((row for row in f if row["effective_ts"] > cross[0]["provider_ts"]), None)
        new_wall = _number(_deep(migration, "values", "call_wall")) if migration else None
        if not migration or new_wall is None or new_wall <= cross[1]: continue
        t0 = migration["effective_ts"]; outcome = _outcome(q, t0)
        record.update({"anchor_t0_utc": t0.isoformat(), "outcome": outcome,
                       "proxy_vwap_basis": "quote_mid_proxy_vwap_not_traded_vwap",
                       "thresholds": {"S_bps": S_BPS, "R_seconds": R_SECONDS, "V_minutes": V_MINUTES,
                                      "acceptance_positive_minutes": 3},
                       "provenance": {"sweep_provider_ts": sweep["provider_ts"].isoformat(),
                                      "reclaim_provider_ts": reclaim["provider_ts"].isoformat(),
                                      "wall_source_hash": cross[2].get("record_hash"),
                                      "wall_source_alignment": cross[2].get("alpaca_alignment"),
                                      "migration_structure_hash": migration.get("record_hash"),
                                      "migration_provider": migration.get("endpoint_provenance"),
                                      "migration_alignment": migration.get("alpaca_alignment")}})
        if outcome["a2_outcome_status"] != "A2-AVAILABLE": return _finish(record, "A2_UNAVAILABLE_MISSING_POINT")
        record.update({"counts_toward_n": True, "episode_status": "A2-AVAILABLE"})
        return _finish(record)
    return _finish(record, "NO_COMPLETE_ORDERED_SIGNATURE")


def append_episode_once(path: Path, row: dict[str, Any]) -> bool:
    key = tuple(row.get(field) for field in ("session_date", "instrument", "arm", "cohort"))
    for old in read_jsonl(path):
        if tuple(old.get(field) for field in ("session_date", "instrument", "arm", "cohort")) == key: return False
    append_jsonl(path, row); return True


def evaluate_session(session_date: str, market_path: Path, flash_path: Path,
                     prereg: Path, output: Path) -> list[dict[str, Any]]:
    market, flash_rows, results = read_jsonl(market_path), read_jsonl(flash_path), []
    for symbol in SYMBOLS:
        all_quotes = all_symbol_quotes(market, symbol)
        quotes = market_quotes(market, symbol, session_date)
        structure = align_structures(structures(flash_rows, symbol), all_quotes)
        for evaluator in (evaluate_d1, evaluate_d2):
            row = evaluator(session_date, symbol, quotes, structure, prereg)
            row.setdefault("provenance", {})["input_summary"] = {
                "quote_count": len(quotes),
                "quote_first_provider_ts": quotes[0]["provider_ts"].isoformat() if quotes else None,
                "quote_last_provider_ts": quotes[-1]["provider_ts"].isoformat() if quotes else None,
                "structures": [{"record_hash": item.get("record_hash"),
                                "effective_ts": item["effective_ts"].isoformat(),
                                "freshness": item.get("freshness"),
                                "alpaca_alignment": item.get("alpaca_alignment"),
                                "endpoints": item.get("endpoint_provenance")}
                               for item in structure],
            }
            annotate_matching(row, quotes, session_date, symbol)
            row.pop("record_hash", None); row["record_hash"] = canonical_hash(row)
            append_episode_once(output, row); results.append(row)
    return results


def time_block(stamp: datetime) -> str:
    local = stamp.astimezone(common.ET)
    return f"{local.hour:02d}:{(local.minute // 30) * 30:02d}"


def volatility_bucket(quotes: list[dict[str, Any]], target: datetime) -> str | None:
    trailing = [q for q in quotes if target - timedelta(minutes=15) <= q["provider_ts"] <= target]
    if len(trailing) < 2: return None
    moves = [abs(math.log(b["mid"] / a["mid"])) * 10000 for a, b in zip(trailing, trailing[1:])]
    value = math.sqrt(sum(move * move for move in moves))
    return "LOW" if value <= 5 else "MID" if value <= 12 else "HIGH"


def control_windows(quotes: list[dict[str, Any]], session_date: str, symbol: str) -> list[dict[str, Any]]:
    opened, closed = common.session_bounds(session_date); controls = []
    target = opened
    while target + timedelta(minutes=60) < closed:
        outcome = common.label_outcome(quotes, target)
        bucket = volatility_bucket(quotes, target)
        if outcome["a2_outcome_status"] == "A2-AVAILABLE" and bucket:
            controls.append({"instrument": symbol, "session_date": session_date,
                             "session_time_block": time_block(target), "realized_vol_bucket": bucket,
                             "return_60m": outcome["returns"]["return_60m"]})
        target += timedelta(minutes=5)
    return controls


def annotate_matching(row: dict[str, Any], quotes: list[dict[str, Any]], session_date: str, symbol: str) -> None:
    row["control_pool"] = control_windows(quotes, session_date, symbol)
    stamp = _ts(row.get("anchor_t0_utc"))
    row["match_key"] = ({"instrument": symbol, "session_time_block": time_block(stamp),
                         "realized_vol_bucket": volatility_bucket(quotes, stamp)} if stamp else None)


def permutation_p(effects: list[float]) -> float | None:
    if not effects: return None
    observed = mean(effects)
    if len(effects) <= 13:
        permutations = 1 << len(effects)
        exceed = sum(
            mean(value if mask & (1 << i) else -value for i, value in enumerate(effects)) >= observed
            for mask in range(permutations)
        )
    else:
        permutations = 9999
        rng = random.Random(canonical_hash(effects))
        exceed = sum(
            mean(value if rng.getrandbits(1) else -value for value in effects) >= observed
            for _ in range(permutations)
        )
    return (exceed + 1) / (permutations + 1)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    report = {"study_id": STUDY_ID, "is_inferential": False, "execution_authority": False, "groups": {}}
    for arm, cohort in (("D1", "H1a"), ("D1", "H1b"), ("D2", "SINGLE")):
        selected = [r for r in rows if r.get("arm") == arm and r.get("cohort") == cohort and r.get("counts_toward_n")]
        matched = []
        for episode in selected:
            key = episode.get("match_key") or {}
            candidates = [control for row in rows for control in row.get("control_pool", [])
                          if control.get("instrument") == episode.get("instrument")
                          and control.get("session_date") != episode.get("session_date")
                          and control.get("session_time_block") == key.get("session_time_block")
                          and control.get("realized_vol_bucket") == key.get("realized_vol_bucket")]
            if candidates:
                matched.append((episode, episode["outcome"]["returns"]["return_60m"] - median(c["return_60m"] for c in candidates)))
        effects = [effect for _episode, effect in matched]
        per = {s: [effect for episode, effect in matched if episode["instrument"] == s] for s in SYMBOLS}
        ordered = sorted(matched, key=lambda pair: (pair[0]["session_date"], pair[0]["instrument"])); folds = []
        for fold in range(4):
            values = [effect for i, (_episode, effect) in enumerate(ordered) if i * 4 // max(1, len(ordered)) == fold]
            folds.append(mean(values) if values else None)
        same = sum(bool(values) and mean(values) > 0 for values in per.values())
        p = permutation_p(effects); n = len(selected)
        verdict = "UNDERPOWERED" if n < TARGET_N or len(effects) < TARGET_N else "EDGE" if mean(effects) > 0 and p is not None and p <= .01 and sum(x is not None and x > 0 for x in folds) >= 3 else "NO EDGE"
        report["groups"][f"{arm}:{cohort}"] = {"n": n, "matched_n": len(effects), "mean_matched_effect_60m": mean(effects) if effects else None,
            "null_p": p, "fold_means": folds, "per_instrument": {s: {"n": len(v), "mean": mean(v) if v else None} for s, v in per.items()},
            "replication": "REPLICATED" if same >= 3 else "NOT_REPLICATED", "verdict": verdict}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("rate-check")
    flash = sub.add_parser("poll-flash"); flash.add_argument("--polls", type=int, required=True); flash.add_argument("--interval-seconds", type=int, default=300); flash.add_argument("--output", type=Path, default=DEFAULT_FLASH_LEDGER)
    market = sub.add_parser("poll-market"); market.add_argument("--polls", type=int, required=True); market.add_argument("--interval-seconds", type=int, default=5); market.add_argument("--output", type=Path, default=DEFAULT_MARKET_LEDGER)
    evaluate = sub.add_parser("evaluate"); evaluate.add_argument("--session-date", required=True); evaluate.add_argument("--market-ledger", type=Path, default=DEFAULT_MARKET_LEDGER); evaluate.add_argument("--flash-ledger", type=Path, default=DEFAULT_FLASH_LEDGER); evaluate.add_argument("--prereg", type=Path, default=DEFAULT_PREREG); evaluate.add_argument("--output", type=Path, default=DEFAULT_STUDY_LEDGER)
    report = sub.add_parser("report"); report.add_argument("--ledger", type=Path, default=DEFAULT_STUDY_LEDGER); report.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    if args.command == "rate-check": print(json.dumps(rate_budget(), sort_keys=True)); return 0 if rate_budget()["fits"] else 2
    if args.command == "poll-flash":
        result = poll_loop(lambda: flash_poll_once(output=args.output, api_key=keychain_key()), polls=args.polls, interval=args.interval_seconds)
    elif args.command == "poll-market":
        key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
        if not key or not secret: raise SystemExit("Alpaca Keychain environment is unavailable")
        result = poll_loop(lambda: alpaca_poll_once(output=args.output, api_key=key, api_secret=secret), polls=args.polls, interval=args.interval_seconds)
    elif args.command == "evaluate": result = {"records": len(evaluate_session(args.session_date, args.market_ledger, args.flash_ledger, args.prereg, args.output))}
    else:
        result = summarize(read_jsonl(args.ledger)); args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == "__main__": raise SystemExit(main())
