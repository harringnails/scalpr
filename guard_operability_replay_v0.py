#!/usr/bin/env python3
"""Deterministic, replay-only simulation of Scalpr's shipped Guard policy."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "guard-operability-counterfactual-v0"
STUDY_STATUS = "EXPLORATORY - NON-INFERENTIAL"
DEFAULT_OUTPUT = Path("guard_operability_counterfactual_v0.jsonl")
DEFAULT_LADDER = ((0.0, 15.0), (15.0, 6.0), (30.0, 2.5))
DEFAULT_TRADES = (
    ("SPY260903C00768000", "1.5200"),
    ("SPY260903C00768000", "4.4900"),
    ("SPY260903P00773000", "0.4400"),
)


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GuardPolicy:
    ladder: tuple[tuple[float, float], ...] = DEFAULT_LADDER
    stall_seconds: float = 45.0
    stall_min_profit: float = 20.0
    grace_seconds: float = 60.0
    confirm_reads: int = 2
    exit_slippage: float = 0.05
    commission_per_contract_per_side: float = 0.65

    def tolerance(self, peak_pct: float) -> float:
        tolerance = self.ladder[0][1]
        for activation, allowed_giveback in self.ladder:
            if peak_pct >= activation:
                tolerance = allowed_giveback
        return tolerance

    def provenance(self) -> dict[str, Any]:
        return {
            "commission_per_contract_per_side_usd": self.commission_per_contract_per_side,
            "confirm_reads": self.confirm_reads,
            "exit_fill_basis": "executable_bid_minus_frozen_slippage",
            "exit_slippage_points": self.exit_slippage,
            "grace_seconds": self.grace_seconds,
            "ladder": [
                {"activation_profit_pct": at, "max_giveback_pct": tol}
                for at, tol in self.ladder
            ],
            "policy": "shipped_whole_position_ratchet_plus_stall",
            "stall_min_profit_pct": self.stall_min_profit,
            "stall_seconds": self.stall_seconds,
        }


def read_quote_series(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["_line_number"] = line_number
            rows.append(row)
    rows.sort(key=lambda row: (_parse_ts(row["ts"]), row["_line_number"]))
    return rows


def _valid_bid(row: dict[str, Any]) -> float | None:
    bid = row.get("option_bid")
    ask = row.get("option_ask")
    if row.get("quote_quality") not in (None, "ok"):
        return None
    if not isinstance(bid, (int, float)) or bid <= 0:
        return None
    if isinstance(ask, (int, float)) and ask < bid:
        return None
    return float(bid)


def replay_guard(
    rows: Iterable[dict[str, Any]],
    *,
    entry_time: str,
    entry_price: float,
    quantity: float,
    policy: GuardPolicy = GuardPolicy(),
) -> dict[str, Any]:
    """Replay using only each current and prior quote; future rows score MFE/MAE only."""
    entry_dt = _parse_ts(entry_time)
    ordered = sorted(rows, key=lambda row: _parse_ts(row["ts"]))
    valid = [
        (row, _parse_ts(row["ts"]), _valid_bid(row))
        for row in ordered
        if _parse_ts(row["ts"]) >= entry_dt
    ]
    valid = [(row, ts, bid) for row, ts, bid in valid if bid is not None]
    if not valid:
        return {"status": "UNAVAILABLE", "unavailable_reason": "NO_VALID_EXECUTABLE_BID"}

    full_returns = [((bid - entry_price) / entry_price) * 100.0 for _, _, bid in valid]
    mfe_pct = max(full_returns)
    mae_pct = min(full_returns)
    peak_pct = 0.0
    peak_time = entry_dt
    breach = 0
    exit_row: dict[str, Any] | None = None
    exit_ts: datetime | None = None
    exit_bid: float | None = None
    exit_reason: str | None = None
    decision_prefix_hash: str | None = None

    for index, (row, ts, bid) in enumerate(valid):
        profit_pct = ((bid - entry_price) / entry_price) * 100.0
        if profit_pct > peak_pct:
            peak_pct = profit_pct
            peak_time = ts
            breach = 0
            continue
        if (ts - entry_dt).total_seconds() < policy.grace_seconds:
            continue
        tolerance = policy.tolerance(peak_pct)
        if profit_pct <= peak_pct - tolerance:
            breach += 1
            if breach < policy.confirm_reads:
                continue
            exit_row, exit_ts, exit_bid = row, ts, bid
            exit_reason = (
                f"dip: {profit_pct:+.3f}% is {peak_pct - profit_pct:.3f}% below "
                f"peak {peak_pct:+.3f}% (tol {tolerance}%, confirmed {breach}x)"
            )
        else:
            breach = 0
            if (
                policy.stall_seconds
                and peak_pct >= policy.stall_min_profit
                and (ts - peak_time).total_seconds() >= policy.stall_seconds
            ):
                exit_row, exit_ts, exit_bid = row, ts, bid
                exit_reason = (
                    f"stall: {policy.stall_seconds:g}s without a new peak above "
                    f"+{policy.stall_min_profit:g}%"
                )
        if exit_row is not None:
            decision_prefix_hash = _hash([
                {key: value for key, value in prior.items() if key != "_line_number"}
                for prior, _, _ in valid[: index + 1]
            ])
            break

    if exit_row is None or exit_ts is None or exit_bid is None:
        return {
            "status": "UNAVAILABLE",
            "unavailable_reason": "NO_GUARD_EXIT_IN_CAPTURE_WINDOW",
            "mae_pct": round(mae_pct, 6),
            "mfe_pct": round(mfe_pct, 6),
        }

    effective_exit = max(0.0, exit_bid - policy.exit_slippage)
    gross_bid_return = ((exit_bid - entry_price) / entry_price) * 100.0
    gross_pnl = (effective_exit - entry_price) * quantity * 100.0
    commissions = policy.commission_per_contract_per_side * quantity * 2.0
    net_pnl = gross_pnl - commissions
    entry_capital = entry_price * quantity * 100.0
    net_return = (net_pnl / entry_capital) * 100.0
    future_returns = [
        ((bid - entry_price) / entry_price) * 100.0
        for _, ts, bid in valid
        if ts > exit_ts
    ]
    continuation_after_exit = max(future_returns, default=float("-inf")) > 0.0
    return {
        "commissions_usd": round(commissions, 2),
        "continuation_after_exit": continuation_after_exit,
        "decision_prefix_hash": decision_prefix_hash,
        "early_exit": bool(net_return < 0 and continuation_after_exit),
        "effective_exit_price": round(effective_exit, 6),
        "exit_bid": round(exit_bid, 6),
        "exit_observation_sequence": exit_row.get("observation_sequence"),
        "exit_reason": exit_reason,
        "exit_time_utc": exit_ts.isoformat(),
        "gross_bid_return_pct": round(gross_bid_return, 6),
        "mae_pct": round(mae_pct, 6),
        "mfe_pct": round(mfe_pct, 6),
        "net_pnl_usd": round(net_pnl, 2),
        "net_return_pct": round(net_return, 6),
        "opportunity_cost_pct": round(mfe_pct - net_return, 6),
        "status": "AVAILABLE",
        "time_in_trade_seconds": round((exit_ts - entry_dt).total_seconds(), 3),
    }


def _journal_trades(path: Path, session_date: str) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not row.get("utc_time", "").startswith(session_date):
                continue
            key = (row.get("symbol"), f"{float(row.get('entry', 0)):.4f}")
            if key in DEFAULT_TRADES:
                selected.append(row)
    return sorted(selected, key=lambda row: _parse_ts(row["utc_time"]))


def _session_paths(paths_dir: Path, symbol: str, session_date: str) -> list[Path]:
    matches: list[tuple[datetime, Path]] = []
    for path in paths_dir.glob(f"{symbol}*.jsonl"):
        first = read_quote_series(path)[:1]
        if first and _parse_ts(first[0]["ts"]).date().isoformat() == session_date:
            matches.append((_parse_ts(first[0]["ts"]), path))
    if not matches:
        raise FileNotFoundError(f"no incubation path for {symbol}")
    return [path for _, path in sorted(matches)]


def run_session(
    *,
    session_date: str,
    journal_path: Path,
    paths_dir: Path,
    snapshots_dir: Path,
    output_path: Path,
    policy: GuardPolicy = GuardPolicy(),
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    trades = _journal_trades(journal_path, session_date)
    paths_by_symbol: dict[str, list[Path]] = {}
    used_by_symbol: dict[str, int] = {}
    for ordinal, trade in enumerate(trades, 1):
        symbol = trade["symbol"]
        paths_by_symbol.setdefault(symbol, _session_paths(paths_dir, symbol, session_date))
        path_index = used_by_symbol.get(symbol, 0)
        if path_index >= len(paths_by_symbol[symbol]):
            raise FileNotFoundError(f"not enough incubation paths for {symbol}")
        path = paths_by_symbol[symbol][path_index]
        used_by_symbol[symbol] = path_index + 1
        snapshot_path = snapshots_dir / f"{path.stem}.json"
        if not snapshot_path.exists():
            raise FileNotFoundError(f"entry snapshot missing for {path.name}")
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        entry_time = snapshot["entry_timestamp"]
        result = replay_guard(
            read_quote_series(path), entry_time=entry_time,
            entry_price=float(trade["entry"]), quantity=float(trade["qty"]), policy=policy,
        )
        record = {
            "actual_paused_outcome": {
                "exit_time_utc": trade["utc_time"],
                "exit_price": float(trade["exit"]),
                "realized_return_pct": float(trade["realized_pct"]),
                "modeled_net_return_pct": round((
                    (
                        (max(0.0, float(trade["exit"]) - policy.exit_slippage)
                         - float(trade["entry"]))
                        * float(trade["qty"]) * 100.0
                        - policy.commission_per_contract_per_side * float(trade["qty"]) * 2.0
                    )
                    / (float(trade["entry"]) * float(trade["qty"]) * 100.0)
                    * 100.0
                ), 6),
            },
            "admission_authority": False,
            "entry_price": float(trade["entry"]),
            "entry_time_utc": entry_time,
            "execution_authority": False,
            "guard_access": False,
            "input_path": str(path),
            "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "snapshot_path": str(snapshot_path),
            "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            "ordinal": ordinal,
            "policy": policy.provenance(),
            "quantity": float(trade["qty"]),
            "record_type": "GUARD_ALWAYS_ACTIVE_REPLAY",
            "schema_version": SCHEMA_VERSION,
            "session_date": session_date,
            "study_status": STUDY_STATUS,
            "symbol": trade["symbol"],
            **result,
        }
        record["record_hash"] = _hash(record)
        records.append(record)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(_canonical(record) + "\n" for record in records)
    output_path.write_text(payload, encoding="utf-8")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="replay the frozen baseline over one session")
    run.add_argument("--session-date", default="2026-09-03")
    run.add_argument("--journal", type=Path, default=Path("scalp_journal.csv"))
    run.add_argument("--paths-dir", type=Path, default=Path("incubation_paths"))
    run.add_argument("--snapshots-dir", type=Path, default=Path("incubation_snapshots"))
    run.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    records = run_session(
        session_date=args.session_date,
        journal_path=args.journal,
        paths_dir=args.paths_dir,
        snapshots_dir=args.snapshots_dir,
        output_path=args.output,
    )
    print(json.dumps({
        "execution_authority": False,
        "output": str(args.output),
        "records": len(records),
        "results": [
            {
                "early_exit": row.get("early_exit"),
                "exit_time_utc": row.get("exit_time_utc"),
                "net_return_pct": row.get("net_return_pct"),
                "status": row["status"],
                "symbol": row["symbol"],
            }
            for row in records
        ],
        "study_status": STUDY_STATUS,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
