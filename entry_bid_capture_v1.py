"""Append-only executable option-bid capture and v1 outcome labeling.

This is a forward-only, paper/shadow research component.  Callers provide quote
observations; this module performs no network requests and has no order authority.
Unavailable/stale quotes are recorded as such and are never carried forward.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import feature_engine as fe


BID_RECORD_VERSION = "entry-intelligence-option-bid-v1"
OUTCOME_VERSION = "entry-intelligence-executable-bid-outcome-v1"
COST_MODEL_VERSION = "entry-intel-cost-model-v1"
COST_MODEL_STATUS = "IMPLEMENTED_RESEARCH_MODEL"
DEFAULT_BID_LOG = Path("entry_intelligence_bid_ticks_v1.jsonl")
DEFAULT_OUTCOME_LOG = Path("entry_intelligence_outcomes_v1.jsonl")
DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS = 3.0


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def validate_cost_model(raw: dict) -> dict:
    """Validate and hash the operator-approved deterministic cost model.

    The executable ask/bid path already pays the spread.  This model adds only
    pass-through fees and slippage beyond the displayed touch.
    """
    model = dict(raw or {})
    if model.get("cost_model_version") != COST_MODEL_VERSION:
        raise ValueError("unsupported or missing transaction cost model")
    if model.get("return_convention") != "decimal_fraction":
        raise ValueError("cost model return convention must be decimal_fraction")
    if float(model.get("commission_per_contract_usd", -1)) != 0.0:
        raise ValueError("v1 self-directed Alpaca commission must be zero")
    fees = float(model.get("regulatory_fees_round_trip_per_contract_usd", -1))
    tick_size = float(model.get("tick_size_usd_per_share", 0))
    primary = int(model.get("slippage_ticks_per_side_primary", -1))
    sensitivity = [int(value) for value in model.get(
        "slippage_sensitivity_ticks_per_side", [])]
    if fees < 0 or tick_size <= 0:
        raise ValueError("cost model fees and tick size are invalid")
    if sensitivity != [0, 1, 2] or primary not in sensitivity:
        raise ValueError("cost model must report 0/1/2 ticks with a listed primary")
    if model.get("spread_handling") != "already_captured_by_ask_entry_bid_exit_do_not_readd":
        raise ValueError("cost model must not double-count the bid/ask spread")
    model["cost_model_hash"] = fe.canonical_hash({
        key: value for key, value in model.items() if key != "cost_model_hash"
    })
    return model


def net_return_sensitivity(*, entry_ask: float, exit_bid: float,
                           cost_model: dict) -> dict:
    """Return cost-adjusted decimal returns at each frozen slippage level."""
    model = validate_cost_model(cost_model)
    entry_ask, exit_bid = float(entry_ask), float(exit_bid)
    if entry_ask <= 0 or exit_bid < 0:
        raise ValueError("cost model requires positive entry ask and nonnegative exit bid")
    tick_size = float(model["tick_size_usd_per_share"])
    fees_per_share = (
        float(model["commission_per_contract_usd"])
        + float(model["regulatory_fees_round_trip_per_contract_usd"])
    ) / 100.0
    values = {}
    for ticks in model["slippage_sensitivity_ticks_per_side"]:
        slippage = int(ticks) * tick_size
        entry_effective = entry_ask + slippage
        exit_effective = max(0.0, exit_bid - slippage)
        net = ((exit_effective - entry_effective) - fees_per_share) / entry_effective
        values[str(int(ticks))] = round(net, 8)
    primary_key = str(int(model["slippage_ticks_per_side_primary"]))
    return {
        "cost_model_version": model["cost_model_version"],
        "cost_model_hash": model["cost_model_hash"],
        "cost_model_status": COST_MODEL_STATUS,
        "net_return_fraction": values[primary_key],
        "net_return_fraction_by_slippage_ticks_per_side": values,
        "primary_slippage_ticks_per_side": int(
            model["slippage_ticks_per_side_primary"]),
    }


def build_bid_record(
    *, decision_id: str, cohort_id: str, option_symbol: str,
    observed_at: Any, received_at: Any, bid: Optional[float], ask: Optional[float],
    max_quote_age_seconds: float, config_version: str, config_hash: str,
    source: str = "alpaca_options_latest_quote:opra",
    provider_sequence: Optional[str] = None, source_available: bool = True,
    clock_skew_tolerance_seconds: float = DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS,
) -> dict:
    observed, received = _utc(observed_at), _utc(received_at)
    age = (received - observed).total_seconds()
    reasons = []
    if not source_available:
        reasons.append("PROVIDER_UNAVAILABLE")
    if age < -float(clock_skew_tolerance_seconds):
        reasons.append("PROVIDER_TIME_AFTER_RECEIPT")
    effective_age = max(0.0, age)
    if effective_age > float(max_quote_age_seconds):
        reasons.append("STALE")
    clean_bid = float(bid) if bid is not None else None
    clean_ask = float(ask) if ask is not None else None
    if clean_bid is None or clean_bid <= 0:
        reasons.append("NO_EXECUTABLE_BID")
    if clean_ask is None or clean_ask <= 0:
        reasons.append("NO_EXECUTABLE_ASK")
    if clean_bid is not None and clean_ask is not None and clean_ask < clean_bid:
        reasons.append("CROSSED_QUOTE")
    fresh = not reasons
    mid = ((clean_bid + clean_ask) / 2 if fresh else None)
    spread_pct = ((clean_ask - clean_bid) / mid * 100 if mid else None)
    identity = {
        "version": BID_RECORD_VERSION, "decision_id": decision_id,
        "option_symbol": option_symbol, "source": source,
        "observed_at": observed.isoformat(timespec="milliseconds"),
        "bid": None if clean_bid is None else format(clean_bid, ".8f"),
        "ask": None if clean_ask is None else format(clean_ask, ".8f"),
        "provider_sequence": provider_sequence,
    }
    return {
        "schema_version": BID_RECORD_VERSION,
        "config_version": config_version, "config_hash": config_hash,
        "quote_id": fe.canonical_hash(identity),
        "decision_id": decision_id, "cohort_id": cohort_id,
        "option_symbol": option_symbol, "source": source,
        "provider_sequence": provider_sequence,
        "observed_at": observed.isoformat(), "received_at": received.isoformat(),
        "quote_age_seconds": round(age, 3),
        "clock_skew_tolerance_seconds": float(clock_skew_tolerance_seconds),
        "status": (
            "UNAVAILABLE" if "PROVIDER_UNAVAILABLE" in reasons else
            "UNUSABLE" if any(reason in reasons for reason in (
                "PROVIDER_TIME_AFTER_RECEIPT", "CROSSED_QUOTE")) else
            "MISSING" if any(reason in reasons for reason in (
                "NO_EXECUTABLE_BID", "NO_EXECUTABLE_ASK")) else
            "STALE" if "STALE" in reasons else "FRESH"
        ),
        "bid": clean_bid, "ask": clean_ask,
        "spread_pct": round(spread_pct, 6) if spread_pct is not None else None,
        "unavailable_reasons": sorted(set(reasons)),
        "execution_authority": False,
    }


def append_bid_record(record: dict, path=DEFAULT_BID_LOG) -> bool:
    """Idempotently append a quote observation by deterministic quote_id."""
    target = Path(path)
    quote_id = record.get("quote_id")
    if not quote_id:
        raise ValueError("quote_id is required")
    if any(row.get("quote_id") == quote_id for row in fe._iter_jsonl(target)):
        return False
    return fe._atomic_append(target, record)


def capture_latest_option_quote(
    *, option_data, decision_id: str, cohort_id: str, option_symbol: str,
    received_at: Any, max_quote_age_seconds: float,
    config_version: str, config_hash: str,
) -> dict:
    """Read one Alpaca latest option quote and return a record; never an order.

    The import stays inside the function so offline tests require no Alpaca
    package and can exercise the record builder directly.
    """
    from alpaca.data.enums import OptionsFeed
    from alpaca.data.requests import OptionLatestQuoteRequest

    response = option_data.get_option_latest_quote(
        OptionLatestQuoteRequest(
            symbol_or_symbols=option_symbol, feed=OptionsFeed.OPRA))
    quote = response.get(option_symbol) if response else None
    observed = getattr(quote, "timestamp", None) or received_at
    return build_bid_record(
        decision_id=decision_id, cohort_id=cohort_id, option_symbol=option_symbol,
        observed_at=observed, received_at=received_at,
        bid=(float(quote.bid_price) if quote and quote.bid_price else None),
        ask=(float(quote.ask_price) if quote and quote.ask_price else None),
        max_quote_age_seconds=max_quote_age_seconds,
        config_version=config_version, config_hash=config_hash,
    )


def records_for_decision(decision_id: str, path=DEFAULT_BID_LOG) -> list[dict]:
    rows = [row for row in fe._iter_jsonl(path) if row.get("decision_id") == decision_id]
    return sorted(rows, key=lambda row: (row.get("observed_at", ""), row.get("quote_id", "")))


def _fresh_rows(decision: dict, records: list[dict], max_gap_seconds: float) -> list[dict]:
    start = _utc(decision["entry_quote_observed_at"])
    end = start + timedelta(minutes=int(decision["outcome_horizon_minutes"]))
    out = []
    seen = set()
    for row in sorted(records or [], key=lambda item: (item.get("observed_at", ""), item.get("quote_id", ""))):
        if row.get("decision_id") != decision.get("decision_id") or row.get("status") != "FRESH":
            continue
        try:
            ts = _utc(row["observed_at"])
        except Exception:
            continue
        if not (start <= ts <= end) or row.get("bid") is None:
            continue
        # If duplicate payloads share a provider timestamp, retain the worst bid.
        key = ts.isoformat()
        if key in seen:
            existing = next(item for item in out if item["_ts"] == ts)
            existing["bid"] = min(float(existing["bid"]), float(row["bid"]))
            continue
        seen.add(key)
        out.append({**row, "bid": float(row["bid"]), "_ts": ts})
    return out


def _coverage(rows: list[dict], start: datetime, horizon_minutes: int,
              poll_interval_seconds: float, max_gap_seconds: float) -> dict:
    if not rows:
        return {"observed": 0, "expected": max(1, int(horizon_minutes * 60 / poll_interval_seconds)),
                "fraction": 0.0, "max_gap_seconds": None}
    expected = max(1, int(horizon_minutes * 60 / poll_interval_seconds))
    gaps = [(rows[i]["_ts"] - rows[i - 1]["_ts"]).total_seconds() for i in range(1, len(rows))]
    first_gap = max(0.0, (rows[0]["_ts"] - start).total_seconds())
    max_gap = max([first_gap, *gaps]) if gaps or first_gap else 0.0
    usable = sum(1 for row in rows if (row.get("quote_age_seconds") or 0) <= max_gap_seconds)
    return {"observed": len(rows), "expected": expected,
            "fraction": round(min(1.0, usable / expected), 6),
            "max_gap_seconds": round(max_gap, 3)}


def evaluate_outcome(decision: dict, records: list[dict], *, evaluated_at: Any) -> dict:
    """Label a native executable-bid path; never midpoint and never imputation."""
    evaluated = _utc(evaluated_at)
    start = _utc(decision["entry_quote_observed_at"])
    horizon_minutes = int(decision["outcome_horizon_minutes"])
    terminal = start + timedelta(minutes=horizon_minutes)
    rows = _fresh_rows(decision, records, float(decision["max_fresh_gap_seconds"]))
    coverage = _coverage(
        rows, start, horizon_minutes, float(decision["bid_poll_interval_seconds"]),
        float(decision["max_fresh_gap_seconds"]),
    )
    cost_model = validate_cost_model(decision.get("cost_model") or {})
    base = {
        "schema_version": OUTCOME_VERSION,
        "config_version": decision["config_version"],
        "config_hash": decision["config_hash"],
        "decision_id": decision["decision_id"], "cohort_id": decision["cohort_id"],
        "option_symbol": decision["option_symbol"],
        "entry_basis": "EXECUTABLE_ASK", "exit_path_basis": "EXECUTABLE_BID",
        "entry_fill_classification": "SIMULATED",
        "broker_fill_observed": False,
        "entry_ask": float(decision["entry_ask"]),
        "target_bid": float(decision["target_bid"]),
        "stop_bid": float(decision["stop_bid"]),
        "coverage": coverage,
        "cost_model_status": COST_MODEL_STATUS,
        "cost_model_version": cost_model["cost_model_version"],
        "cost_model_hash": cost_model["cost_model_hash"],
        "net_return_after_realistic_costs": None,
        "net_return_fraction": None,
        "net_return_fraction_by_slippage_ticks_per_side": {
            str(value): None
            for value in cost_model["slippage_sensitivity_ticks_per_side"]
        },
        "primary_slippage_ticks_per_side": int(
            cost_model["slippage_ticks_per_side_primary"]),
        "evaluated_at": evaluated.isoformat(),
        "execution_authority": False,
    }
    if not rows:
        status = "PENDING" if evaluated < terminal else "UNLABELABLE_NO_EXECUTABLE_BIDS"
        return {**base, "status": status, "first_hit": None,
                "gross_executable_return": None, "mfe": None, "mae": None,
                "horizon_returns": {str(h): None for h in decision["return_horizons_minutes"]}}

    entry = float(decision["entry_ask"])
    target, stop = float(decision["target_bid"]), float(decision["stop_bid"])
    first_hit = None
    first_hit_at = None
    first_hit_bid = None
    # Grouping by provider timestamp above plus worst-bid retention makes an
    # ambiguous timestamp conservative: stop is checked before target.
    for row in rows:
        if row["bid"] <= stop:
            first_hit, first_hit_at, first_hit_bid = "STOP_FIRST", row["_ts"], row["bid"]
            break
        if row["bid"] >= target:
            first_hit, first_hit_at, first_hit_bid = "TARGET_FIRST", row["_ts"], row["bid"]
            break

    def ret(bid):
        return (float(bid) - entry) / entry

    returns = [ret(row["bid"]) for row in rows]
    horizons = {}
    tolerance = float(decision["max_fresh_gap_seconds"])
    for minutes in decision["return_horizons_minutes"]:
        due = start + timedelta(minutes=int(minutes))
        candidates = [row for row in rows if due <= row["_ts"] <= due + timedelta(seconds=tolerance)]
        horizons[str(minutes)] = round(ret(candidates[0]["bid"]), 8) if candidates else None

    if first_hit:
        status = "FINAL"
        terminal_bid = first_hit_bid
    elif evaluated < terminal:
        status = "PENDING"
        terminal_bid = rows[-1]["bid"]
    elif coverage["fraction"] < float(decision["minimum_coverage_fraction"]):
        status = "UNLABELABLE_INSUFFICIENT_COVERAGE"
        terminal_bid = None
    else:
        status = "FINAL"
        terminal_bid = rows[-1]["bid"]

    cost_result = (net_return_sensitivity(
        entry_ask=entry, exit_bid=terminal_bid, cost_model=cost_model)
        if terminal_bid is not None else None)
    return {
        **base, "status": status, "first_hit": first_hit,
        "first_hit_at": first_hit_at.isoformat() if first_hit_at else None,
        "gross_executable_return": round(ret(terminal_bid), 8) if terminal_bid is not None else None,
        "net_return_after_realistic_costs": (
            cost_result["net_return_fraction"] if cost_result else None),
        "net_return_fraction": (
            cost_result["net_return_fraction"] if cost_result else None),
        "net_return_fraction_by_slippage_ticks_per_side": (
            cost_result["net_return_fraction_by_slippage_ticks_per_side"]
            if cost_result else base["net_return_fraction_by_slippage_ticks_per_side"]),
        "primary_slippage_ticks_per_side": (
            cost_result["primary_slippage_ticks_per_side"] if cost_result else
            int(cost_model["slippage_ticks_per_side_primary"])),
        "mfe": round(max(returns), 8), "mae": round(min(returns), 8),
        "horizon_returns": horizons,
    }


def append_outcome(record: dict, path=DEFAULT_OUTCOME_LOG) -> bool:
    """Append lifecycle records; never overwrite PENDING with an in-place edit."""
    payload = dict(record)
    payload["outcome_record_id"] = fe.canonical_hash({
        "version": OUTCOME_VERSION, "decision_id": payload.get("decision_id"),
        "status": payload.get("status"), "evaluated_at": payload.get("evaluated_at"),
        "first_hit": payload.get("first_hit"), "coverage": payload.get("coverage"),
    })
    if any(row.get("outcome_record_id") == payload["outcome_record_id"] for row in fe._iter_jsonl(path)):
        return False
    return fe._atomic_append(path, payload)
