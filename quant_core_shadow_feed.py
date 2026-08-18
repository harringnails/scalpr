"""Default-off Quant Core OPRA observer for Scalpr shadow evidence.

This sidecar has no broker, Guard, order, or liquidation imports. It creates a
Scalpr-consumer view, preserves provider/receipt timestamps, and writes only
append-only evidence with execution authority permanently disabled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA_VERSION = "scalpr-quant-core-opra-shadow-v1"
ENABLE_ENV = "SCALPR_QUANT_CORE_SHADOW_ENABLED"
DEFAULT_LOG = Path("quant_core_opra_shadow_v1.jsonl")
DEFAULT_RECONCILIATION_LOG = Path("quant_core_opra_reconciliation_v1.jsonl")
AUTOMATED_MAX_DTE = 2
APPROVED_MAX_SPREAD_PCT = 6.0
LIVE_MAX_QUOTE_AGE_MS = 5_000
NEW_YORK = ZoneInfo("America/New_York")


class ShadowFeedDisabled(RuntimeError):
    """Raised when the operator has not explicitly enabled the sidecar."""


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_enabled(environ: Mapping[str, str] | None = None) -> None:
    current = os.environ if environ is None else environ
    if current.get(ENABLE_ENV) != "1":
        raise ShadowFeedDisabled(
            f"Quant Core shadow feed is default-off; set {ENABLE_ENV}=1 explicitly"
        )


def _consumer_name(view: object) -> str:
    consumer = getattr(view, "consumer", None)
    return str(getattr(consumer, "value", consumer))


def build_shadow_record(
    view: object,
    *,
    mode: str,
    captured_at: datetime,
    previous_record_hash: str | None = None,
) -> dict[str, Any]:
    """Build deterministic, non-executable Scalpr evidence from one consumer view."""

    if mode not in {"historical_replay", "live_observer"}:
        raise ValueError("unsupported Quant Core shadow mode")
    if captured_at.tzinfo is None:
        raise ValueError("captured_at must be timezone-aware")
    if _consumer_name(view) != "scalpr":
        raise ValueError("Quant Core shadow record requires a Scalpr consumer view")
    if getattr(view, "order_routing_permitted", True):
        raise ValueError("Quant Core Scalpr shadow view must prohibit order routing")

    as_of = view.as_of_timestamp
    market_date = as_of.astimezone(NEW_YORK).date()
    oi_by_contract = {item.contract_id: item for item in view.open_interest}
    contracts: list[dict[str, Any]] = []
    for quote in view.quotes:
        if quote.underlying != "SPY":
            continue
        dte = (quote.expiration - market_date).days
        if dte < 0 or dte > AUTOMATED_MAX_DTE:
            continue
        bid = float(quote.bid)
        ask = float(quote.ask)
        mid = (bid + ask) / 2.0
        two_sided = bid > 0 and ask > 0 and ask >= bid
        spread_pct = ((ask - bid) / mid * 100.0) if two_sided and mid > 0 else None
        spread_pass = (
            spread_pct is not None and spread_pct <= APPROVED_MAX_SPREAD_PCT
        )
        resolution = str(getattr(quote.resolution, "value", quote.resolution))
        live_gate_pass = (
            two_sided
            and spread_pass
            and resolution == "tick"
            and quote.quote_age_ms <= LIVE_MAX_QUOTE_AGE_MS
        )
        oi = oi_by_contract.get(quote.contract_id)
        contracts.append(
            {
                "contract_id": quote.contract_id,
                "expiration": quote.expiration.isoformat(),
                "dte": dte,
                "strike": str(quote.strike),
                "option_type": str(
                    getattr(quote.option_type, "value", quote.option_type)
                ),
                "bid": str(quote.bid),
                "ask": str(quote.ask),
                "bid_size": quote.bid_size,
                "ask_size": quote.ask_size,
                "observed_at": quote.source_timestamp.isoformat(),
                "received_at": quote.ingest_timestamp.isoformat(),
                "quote_age_ms": quote.quote_age_ms,
                "resolution": resolution,
                "spread_pct": (
                    round(spread_pct, 6) if spread_pct is not None else None
                ),
                "two_sided": two_sided,
                "approved_spread_pass": spread_pass,
                "live_gate_pass": live_gate_pass,
                "open_interest": oi.oi if oi is not None else None,
                "open_interest_business_date": (
                    oi.source_business_date.isoformat() if oi is not None else None
                ),
            }
        )
    contracts.sort(key=lambda item: item["contract_id"])
    counts = {
        "automated_scope_quotes": len(contracts),
        "two_sided_quotes": sum(item["two_sided"] for item in contracts),
        "approved_spread_quotes": sum(
            item["approved_spread_pass"] for item in contracts
        ),
        "live_gate_pass_quotes": sum(item["live_gate_pass"] for item in contracts),
        "open_interest_covered_quotes": sum(
            item["open_interest"] is not None for item in contracts
        ),
    }
    if not contracts:
        status = "INSUFFICIENT_DATA"
    elif mode == "live_observer" and counts["live_gate_pass_quotes"] == 0:
        status = "DEGRADED"
    elif mode == "live_observer":
        status = "OBSERVER_READY"
    else:
        status = "RESEARCH_READY"

    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "status": status,
        "dataset": view.dataset,
        "consumer": "scalpr",
        "snapshot_id": view.snapshot_id,
        "as_of_timestamp": as_of.isoformat(),
        "captured_at": captured_at.astimezone(UTC).isoformat(),
        "scope": {
            "underlying": "SPY",
            "minimum_dte": 0,
            "maximum_dte": AUTOMATED_MAX_DTE,
            "approved_max_spread_pct": APPROVED_MAX_SPREAD_PCT,
            "live_max_quote_age_ms": LIVE_MAX_QUOTE_AGE_MS,
        },
        "counts": counts,
        "contracts": contracts,
        "previous_record_hash": previous_record_hash,
        "execution_authority": False,
        "order_routing_permitted": False,
        "replaces_alpaca": False,
    }
    body["record_hash"] = _canonical_hash(body)
    return body


def _read_evidence(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid shadow evidence JSON at line {line_number}") from exc
        recorded_hash = row.pop("record_hash", None)
        if recorded_hash != _canonical_hash(row):
            raise ValueError(f"shadow evidence hash mismatch at line {line_number}")
        row["record_hash"] = recorded_hash
        if rows and row.get("previous_record_hash") != rows[-1].get("record_hash"):
            raise ValueError(f"shadow evidence chain mismatch at line {line_number}")
        rows.append(row)
    return rows


def append_shadow_record(record: Mapping[str, Any], path: str | Path = DEFAULT_LOG) -> bool:
    """Append once by snapshot/mode while validating the existing hash chain."""

    target = Path(path)
    rows = _read_evidence(target)
    if any(
        row.get("snapshot_id") == record.get("snapshot_id")
        and row.get("mode") == record.get("mode")
        for row in rows
    ):
        return False
    expected_previous = rows[-1]["record_hash"] if rows else None
    if record.get("previous_record_hash") != expected_previous:
        raise ValueError("new shadow record does not extend the existing evidence chain")
    candidate = dict(record)
    recorded_hash = candidate.pop("record_hash", None)
    if recorded_hash != _canonical_hash(candidate):
        raise ValueError("new shadow record hash is invalid")
    candidate["record_hash"] = recorded_hash
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as destination:
        destination.write(json.dumps(candidate, sort_keys=True, separators=(",", ":")))
        destination.write("\n")
        destination.flush()
        os.fsync(destination.fileno())
    return True


def _previous_hash(path: Path) -> str | None:
    rows = _read_evidence(path)
    return rows[-1]["record_hash"] if rows else None


def _parse_timestamp(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("reconciliation timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def build_reconciliation_record(
    shadow_record: Mapping[str, Any],
    alpaca_rows: list[dict[str, Any]],
    *,
    captured_at: datetime,
    tolerance_seconds: float,
    previous_record_hash: str | None = None,
) -> dict[str, Any]:
    """Match Quant Core observations to read-only Alpaca OPRA evidence."""

    if tolerance_seconds <= 0 or tolerance_seconds > 300:
        raise ValueError("reconciliation tolerance must be in (0, 300]")
    if shadow_record.get("execution_authority") is not False:
        raise ValueError("reconciliation requires non-executable Quant Core evidence")
    if shadow_record.get("order_routing_permitted") is not False:
        raise ValueError("reconciliation cannot consume order-authorized evidence")

    alpaca_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in alpaca_rows:
        source = str(row.get("source", ""))
        if row.get("status") != "FRESH" or not source.endswith(":opra"):
            continue
        symbol = str(row.get("option_symbol", ""))
        if not symbol or row.get("bid") is None or row.get("ask") is None:
            continue
        try:
            observed = _parse_timestamp(row.get("observed_at"))
            bid = float(row["bid"])
            ask = float(row["ask"])
        except (TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        alpaca_by_symbol.setdefault(symbol, []).append(
            {**row, "_observed": observed, "_bid": bid, "_ask": ask}
        )
    for rows in alpaca_by_symbol.values():
        rows.sort(key=lambda item: (item["_observed"], str(item.get("quote_id", ""))))

    matches: list[dict[str, Any]] = []
    for contract in shadow_record.get("contracts", []):
        symbol = str(contract.get("contract_id", ""))
        try:
            quant_time = _parse_timestamp(contract.get("observed_at"))
            quant_bid = float(contract["bid"])
            quant_ask = float(contract["ask"])
        except (TypeError, ValueError):
            continue
        candidates = alpaca_by_symbol.get(symbol, [])
        if not candidates:
            continue
        selected = min(
            candidates,
            key=lambda item: (
                abs((item["_observed"] - quant_time).total_seconds()),
                item["_observed"],
                str(item.get("quote_id", "")),
            ),
        )
        gap = abs((selected["_observed"] - quant_time).total_seconds())
        if gap > tolerance_seconds:
            continue
        alpaca_bid = selected["_bid"]
        alpaca_ask = selected["_ask"]
        matches.append(
            {
                "contract_id": symbol,
                "quant_core_observed_at": quant_time.isoformat(),
                "alpaca_observed_at": selected["_observed"].isoformat(),
                "timestamp_gap_seconds": round(gap, 6),
                "quant_core_bid": quant_bid,
                "quant_core_ask": quant_ask,
                "alpaca_bid": alpaca_bid,
                "alpaca_ask": alpaca_ask,
                "bid_difference": round(quant_bid - alpaca_bid, 8),
                "ask_difference": round(quant_ask - alpaca_ask, 8),
                "mid_difference": round(
                    (quant_bid + quant_ask - alpaca_bid - alpaca_ask) / 2.0,
                    8,
                ),
                "alpaca_quote_id": selected.get("quote_id"),
            }
        )
    matches.sort(key=lambda item: item["contract_id"])
    bid_differences = [item["bid_difference"] for item in matches]
    ask_differences = [item["ask_difference"] for item in matches]
    mid_differences = [item["mid_difference"] for item in matches]
    quant_contract_count = len(shadow_record.get("contracts", []))
    body: dict[str, Any] = {
        "schema_version": "scalpr-quant-core-opra-reconciliation-v1",
        "mode": "reconciliation",
        "status": "MEASURED" if matches else "INSUFFICIENT_OVERLAP",
        "snapshot_id": shadow_record["snapshot_id"],
        "as_of_timestamp": shadow_record["as_of_timestamp"],
        "captured_at": captured_at.astimezone(UTC).isoformat(),
        "tolerance_seconds": tolerance_seconds,
        "counts": {
            "quant_core_contracts": quant_contract_count,
            "eligible_alpaca_rows": sum(len(rows) for rows in alpaca_by_symbol.values()),
            "matched_contracts": len(matches),
        },
        "coverage_fraction": (
            round(len(matches) / quant_contract_count, 8) if quant_contract_count else 0.0
        ),
        "metrics": {
            "median_signed_bid_difference": median(bid_differences) if matches else None,
            "median_signed_ask_difference": median(ask_differences) if matches else None,
            "median_signed_mid_difference": median(mid_differences) if matches else None,
            "median_absolute_mid_difference": (
                median(abs(value) for value in mid_differences) if matches else None
            ),
        },
        "matches": matches,
        "previous_record_hash": previous_record_hash,
        "promotion_eligible": False,
        "execution_authority": False,
        "order_routing_permitted": False,
        "replaces_alpaca": False,
    }
    body["record_hash"] = _canonical_hash(body)
    return body


def capture_reconciliation(
    *,
    shadow_log: str | Path,
    alpaca_log: str | Path,
    output_log: str | Path = DEFAULT_RECONCILIATION_LOG,
    tolerance_seconds: float = 5.0,
) -> tuple[dict[str, Any], bool]:
    _require_enabled()
    shadow_rows = _read_evidence(Path(shadow_log))
    if not shadow_rows:
        raise ValueError("reconciliation requires at least one Quant Core shadow record")
    alpaca_path = Path(alpaca_log)
    if not alpaca_path.is_file():
        raise ValueError("Alpaca OPRA evidence log does not exist")
    alpaca_rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        alpaca_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            alpaca_rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid Alpaca evidence JSON at line {line_number}") from exc
    target = Path(output_log)
    record = build_reconciliation_record(
        shadow_rows[-1],
        alpaca_rows,
        captured_at=datetime.now(UTC),
        tolerance_seconds=tolerance_seconds,
        previous_record_hash=_previous_hash(target),
    )
    return record, append_shadow_record(record, target)


def capture_historical_packet(
    packet_dir: str | Path,
    *,
    output_log: str | Path = DEFAULT_LOG,
    captured_at: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    _require_enabled()
    try:
        from adapters.scalpr import accept_market_data
        from quant_core.enums import ConsumerApplication
        from quant_core.market_data import replay_opra_packet
    except ModuleNotFoundError as exc:
        raise RuntimeError("Quant Core market-data package is unavailable") from exc

    result = replay_opra_packet(packet_dir)
    view = accept_market_data(
        result.snapshot.for_consumer(ConsumerApplication.SCALPR)
    )
    target = Path(output_log)
    record = build_shadow_record(
        view,
        mode="historical_replay",
        captured_at=captured_at or datetime.now(UTC),
        previous_record_hash=_previous_hash(target),
    )
    return record, append_shadow_record(record, target)


def capture_live_observer(
    *,
    seconds: float,
    output_log: str | Path = DEFAULT_LOG,
) -> tuple[dict[str, Any], bool]:
    _require_enabled()
    if seconds <= 0 or seconds > 300:
        raise ValueError("live observer seconds must be in (0, 300]")
    try:
        from adapters.scalpr import accept_market_data
        from quant_core.enums import ConsumerApplication
        from quant_core.market_data import DatabentoOPRALiveService
    except ModuleNotFoundError as exc:
        raise RuntimeError("Quant Core market-data package is unavailable") from exc

    service = DatabentoOPRALiveService()
    service.connect()
    service.run(timeout_seconds=seconds)
    now = datetime.now(UTC)
    snapshot = service.state.snapshot(as_of_timestamp=now)
    view = accept_market_data(snapshot.for_consumer(ConsumerApplication.SCALPR))
    target = Path(output_log)
    record = build_shadow_record(
        view,
        mode="live_observer",
        captured_at=now,
        previous_record_hash=_previous_hash(target),
    )
    return record, append_shadow_record(record, target)


def _summary(record: Mapping[str, Any], appended: bool, output_log: Path) -> dict[str, Any]:
    return {
        "schema_version": record["schema_version"],
        "mode": record["mode"],
        "status": record["status"],
        "snapshot_id": record["snapshot_id"],
        "as_of_timestamp": record["as_of_timestamp"],
        "counts": record["counts"],
        "record_hash": record["record_hash"],
        "appended": appended,
        "output_log": str(output_log),
        "execution_authority": False,
        "order_routing_permitted": False,
        "replaces_alpaca": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    historical = subcommands.add_parser("historical")
    historical.add_argument("--packet-dir", type=Path, required=True)
    historical.add_argument("--output-log", type=Path, default=DEFAULT_LOG)
    live = subcommands.add_parser("live")
    live.add_argument("--seconds", type=float, default=30.0)
    live.add_argument("--output-log", type=Path, default=DEFAULT_LOG)
    reconcile = subcommands.add_parser("reconcile")
    reconcile.add_argument("--shadow-log", type=Path, default=DEFAULT_LOG)
    reconcile.add_argument("--alpaca-log", type=Path, required=True)
    reconcile.add_argument(
        "--output-log", type=Path, default=DEFAULT_RECONCILIATION_LOG
    )
    reconcile.add_argument("--tolerance-seconds", type=float, default=5.0)
    args = parser.parse_args()
    if args.command == "historical":
        record, appended = capture_historical_packet(
            args.packet_dir, output_log=args.output_log
        )
    elif args.command == "live":
        record, appended = capture_live_observer(
            seconds=args.seconds, output_log=args.output_log
        )
    else:
        record, appended = capture_reconciliation(
            shadow_log=args.shadow_log,
            alpaca_log=args.alpaca_log,
            output_log=args.output_log,
            tolerance_seconds=args.tolerance_seconds,
        )
    print(json.dumps(_summary(record, appended, args.output_log), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
