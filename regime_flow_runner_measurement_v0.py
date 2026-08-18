"""Append-only paired measurement for the PAPER regime-flow runner.

This module has no broker, Guard, order, or exit imports.  It only receives
confirmed marks and already-completed lifecycle events, then records the
pre-registered static-ladder counterfactual when the position has closed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import threading
import uuid
from typing import Any, Iterable


MEASUREMENT_VERSION = "regime-flow-runner-measurement-v0"
POOL_PATH = Path("regime_flow_runner_shadow_v0.jsonl")
ACTIVATION_FLOOR = 30
VERDICT_FLOOR = 50
NULL_PERMUTATIONS = 10_000


def _utc(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_seconds(value: Any, now: datetime) -> float | None:
    if not value:
        return None
    try:
        age = (now - _utc(value)).total_seconds()
    except (TypeError, ValueError):
        return None
    return round(age, 6) if age >= 0 else None


def _static_tolerance(ladder: list[dict], peak_pct: float) -> float:
    tolerance = float(ladder[0]["tol"])
    for rung in ladder:
        if peak_pct >= float(rung["at"]):
            tolerance = float(rung["tol"])
    return tolerance


def replay_static_ladder(
    *, entry_price: float, ladder: Iterable[dict], marks: Iterable[dict],
    opened_at: datetime | str, grace_seconds: float, confirm_ticks: int,
    stall_seconds: float = 0.0, stall_min_profit: float = 20.0,
) -> dict[str, Any]:
    """Replay the unmodified normal Guard ratchet on confirmed marks only."""
    normalized_ladder = sorted(
        [{"at": float(rung["at"]), "tol": float(rung["tol"])} for rung in ladder],
        key=lambda rung: rung["at"],
    )
    if not normalized_ladder:
        return {"status": "UNAVAILABLE", "reason": "missing_static_ladder"}
    try:
        entry = float(entry_price)
        opened = _utc(opened_at)
    except (TypeError, ValueError):
        return {"status": "UNAVAILABLE", "reason": "invalid_entry_or_open_time"}
    if not math.isfinite(entry) or entry <= 0:
        return {"status": "UNAVAILABLE", "reason": "invalid_entry_or_open_time"}

    peak = 0.0
    peak_at = opened
    breach = 0
    for mark in marks:
        try:
            price = float(mark["price"])
            observed_at = _utc(mark["observed_at"])
        except (KeyError, TypeError, ValueError):
            return {"status": "UNAVAILABLE", "reason": "invalid_confirmed_mark"}
        if not math.isfinite(price) or price <= 0:
            return {"status": "UNAVAILABLE", "reason": "invalid_confirmed_mark"}
        profit = (price - entry) / entry * 100.0
        if profit > peak:
            peak, peak_at, breach = profit, observed_at, 0
            continue
        if (observed_at - opened).total_seconds() < float(grace_seconds):
            continue
        tolerance = _static_tolerance(normalized_ladder, peak)
        if profit <= peak - tolerance:
            breach += 1
            if breach >= max(1, int(confirm_ticks)):
                return {
                    "status": "AVAILABLE",
                    "exit_price": round(price, 8),
                    "exit_pct": round(profit, 8),
                    "exit_timestamp": observed_at.isoformat(),
                    "exit_reason": "static_ladder_dip",
                    "peak_pct": round(peak, 8),
                    "tolerance_pct": round(tolerance, 8),
                }
            continue
        breach = 0
        if (stall_seconds and peak >= float(stall_min_profit)
                and (observed_at - peak_at).total_seconds() >= float(stall_seconds)):
            return {
                "status": "AVAILABLE",
                "exit_price": round(price, 8),
                "exit_pct": round(profit, 8),
                "exit_timestamp": observed_at.isoformat(),
                "exit_reason": "static_ladder_stall",
                "peak_pct": round(peak, 8),
                "tolerance_pct": round(_static_tolerance(normalized_ladder, peak), 8),
            }
    return {"status": "UNAVAILABLE", "reason": "static_ladder_not_reached_on_recorded_path"}


def _activation_context(observation: dict | None, observed_at: datetime) -> dict[str, Any]:
    observation = observation if isinstance(observation, dict) else {}
    regime = observation.get("regime") if isinstance(observation.get("regime"), dict) else {}
    flow = observation.get("flow") if isinstance(observation.get("flow"), dict) else {}
    metadata = regime.get("metadata") if isinstance(regime.get("metadata"), dict) else {}
    fit_end = metadata.get("fit_end_time")
    flow_as_of = flow.get("as_of")
    return {
        "regime_state": regime.get("state"),
        "regime_status": regime.get("status"),
        "regime_age_seconds": _age_seconds(fit_end, observed_at),
        "flow_tier": flow.get("tier"),
        "flow_direction": flow.get("direction"),
        "flow_age_seconds": _age_seconds(flow_as_of, observed_at),
        "source_signature": "|".join(str(value or "") for value in (
            fit_end, regime.get("state"), flow_as_of, flow.get("tier"), flow.get("direction"),
        )),
    }


def _record_id(record: dict) -> str:
    material = {
        "position_id": record.get("position_id"),
        "runner_exit_timestamp": record.get("runner_exit_timestamp"),
        "measurement_version": record.get("measurement_version"),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def append_record(record: dict[str, Any], *, path: Path = POOL_PATH) -> bool:
    """Append one finalized paired outcome idempotently; never raise upstream."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        record_id = str(record.get("record_id") or "")
        if not record_id:
            return False
        if path.exists():
            with path.open(encoding="utf-8") as source:
                for line in source:
                    if line.strip() and json.loads(line).get("record_id") == record_id:
                        return False
        with path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(record, sort_keys=True) + "\n")
        return True
    except Exception:
        return False


class RunnerMeasurement:
    """Thread-safe, record-only state attached to one runner-eligible position."""

    def __init__(
        self, *, symbol: str, kind: str, entry_price: float, quantity: float,
        opened_at: datetime | str, ladder: Iterable[dict], grace_seconds: float,
        confirm_ticks: int, stall_seconds: float, stall_min_profit: float,
        runner_policy: dict, position_direction: str | None, path: Path = POOL_PATH,
    ):
        self.position_id = f"rfr-shadow-v0-{uuid.uuid4().hex}"
        self.symbol = str(symbol).upper()
        self.kind = str(kind)
        self.entry_price = float(entry_price)
        self.quantity = float(quantity)
        self.opened_at = _utc(opened_at)
        self.ladder = [dict(rung) for rung in ladder]
        self.grace_seconds = float(grace_seconds)
        self.confirm_ticks = max(1, int(confirm_ticks))
        self.stall_seconds = float(stall_seconds)
        self.stall_min_profit = float(stall_min_profit)
        self.runner_policy = dict(runner_policy or {})
        self.position_direction = position_direction
        self.path = Path(path)
        self.marks: list[dict[str, Any]] = []
        self.activation_events: list[dict[str, Any]] = []
        self.last_gate_context: dict[str, Any] | None = None
        self.runner_activated = False
        self.finalized = False
        self._path_peak_pct = 0.0
        self._lock = threading.Lock()

    def record_mark(self, *, price: float, observed_at: datetime | str | None = None,
                    runner_snapshot: dict | None = None) -> None:
        """Record one already-confirmed executable mark without controlling it."""
        try:
            mark_price = float(price)
            timestamp = _utc(observed_at)
        except (TypeError, ValueError):
            return
        if not math.isfinite(mark_price) or mark_price <= 0:
            return
        with self._lock:
            if self.finalized:
                return
            profit = (mark_price - self.entry_price) / self.entry_price * 100.0
            self._path_peak_pct = max(self._path_peak_pct, profit)
            snapshot = dict(runner_snapshot or {})
            self.marks.append({
                "observed_at": timestamp.isoformat(),
                "price": round(mark_price, 8),
                "profit_pct": round(profit, 8),
                "running_peak_pct": round(self._path_peak_pct, 8),
                "giveback_pct": round(self._path_peak_pct - profit, 8),
                "runner_status": snapshot.get("status"),
                "baseline_tolerance_pct": snapshot.get("baseline_tolerance_pct"),
                "effective_tolerance_pct": snapshot.get("effective_tolerance_pct"),
            })

    def record_runner_transition(
        self, *, before_status: str | None, after_snapshot: dict | None,
        observation: dict | None, observed_at: datetime | str | None = None,
    ) -> None:
        """Capture status transitions and the evidence context that caused them."""
        timestamp = _utc(observed_at)
        snapshot = dict(after_snapshot or {})
        context = _activation_context(observation, timestamp)
        with self._lock:
            if self.finalized:
                return
            self.last_gate_context = context
            after_status = snapshot.get("status")
            if before_status == after_status:
                return
            event = {
                "observed_at": timestamp.isoformat(),
                "event": (
                    "RUNNER_ENGAGED" if after_status == "ACTIVE"
                    else "RUNNER_DISENGAGED" if before_status == "ACTIVE"
                    else "RUNNER_STATUS_CHANGED"
                ),
                "before_status": before_status,
                "after_status": after_status,
                "reason_codes": list(snapshot.get("reason_codes") or []),
                **context,
            }
            self.activation_events.append(event)
            if after_status == "ACTIVE":
                self.runner_activated = True

    def finalize(
        self, *, runner_exit_price: float | None, exit_reason: str,
        exited_at: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        """Write the paired result after an exit has already been confirmed."""
        timestamp = _utc(exited_at)
        with self._lock:
            if self.finalized:
                return None
            self.finalized = True
            marks = list(self.marks)
            events = list(self.activation_events)
            runner_activated = self.runner_activated
            last_gate_context = dict(self.last_gate_context or {})
        static = replay_static_ladder(
            entry_price=self.entry_price, ladder=self.ladder, marks=marks,
            opened_at=self.opened_at, grace_seconds=self.grace_seconds,
            confirm_ticks=self.confirm_ticks, stall_seconds=self.stall_seconds,
            stall_min_profit=self.stall_min_profit,
        )
        try:
            exit_price = float(runner_exit_price)
        except (TypeError, ValueError):
            exit_price = None
        runner_exit_pct = (
            round((exit_price - self.entry_price) / self.entry_price * 100.0, 8)
            if exit_price is not None and math.isfinite(exit_price) and exit_price > 0 else None
        )
        status = "AVAILABLE"
        unavailable_reason = None
        if not marks:
            status, unavailable_reason = "UNAVAILABLE", "missing_confirmed_marks"
        elif runner_exit_pct is None:
            status, unavailable_reason = "UNAVAILABLE", "missing_runner_exit_mark"
        elif static.get("status") != "AVAILABLE":
            status, unavailable_reason = "UNAVAILABLE", static.get("reason")

        if not runner_activated and status == "AVAILABLE":
            outcome_tag = "NO_ACTIVATION"
            # With no ACTIVE state, the static and runner exit order is identical.
            static_exit_price = exit_price
            static_exit_pct = runner_exit_pct
            static_exit_timestamp = timestamp.isoformat()
            delta = 0.0
        elif status == "AVAILABLE":
            outcome_tag = "PAIRED"
            static_exit_price = static["exit_price"]
            static_exit_pct = static["exit_pct"]
            static_exit_timestamp = static["exit_timestamp"]
            delta = round(runner_exit_pct - static_exit_pct, 8)
        else:
            outcome_tag = "UNAVAILABLE"
            static_exit_price = static_exit_pct = static_exit_timestamp = delta = None

        record = {
            "record_type": "REGIME_FLOW_RUNNER_PAIRED_OUTCOME",
            "measurement_version": MEASUREMENT_VERSION,
            "position_id": self.position_id,
            "symbol": self.symbol,
            "kind": self.kind,
            "paper_only": True,
            "execution_authority": False,
            "guard_authority": False,
            "order_authority": False,
            "exit_authority": False,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "opened_at": self.opened_at.isoformat(),
            "position_direction": self.position_direction,
            "runner_policy_version": self.runner_policy.get("version"),
            "runner_policy": self.runner_policy,
            "static_ladder": self.ladder,
            "confirmed_mark_path": marks,
            "activation_events": events,
            "last_gate_context": last_gate_context,
            "runner_activated": runner_activated,
            "paired_outcome_status": status,
            "paired_outcome_tag": outcome_tag,
            "unavailable_reason": unavailable_reason,
            "runner_exit_price": round(exit_price, 8) if exit_price is not None else None,
            "runner_exit_pct": runner_exit_pct,
            "runner_exit_timestamp": timestamp.isoformat(),
            "runner_exit_reason": exit_reason,
            "static_exit_price": static_exit_price,
            "static_exit_pct": static_exit_pct,
            "static_exit_timestamp": static_exit_timestamp,
            "static_exit_reason": static.get("exit_reason"),
            "static_replay": static,
            "paired_delta_runner_minus_static_pct": delta,
        }
        record["record_id"] = _record_id(record)
        record["record_appended"] = append_record(record, path=self.path)
        return record


def analyze_records(records: Iterable[dict[str, Any]], *, permutations: int = NULL_PERMUTATIONS) -> dict[str, Any]:
    """Apply the pre-registered paired analysis to finalized pool records."""
    paired = [
        row for row in records
        if row.get("record_type") == "REGIME_FLOW_RUNNER_PAIRED_OUTCOME"
    ]
    available = [row for row in paired if row.get("paired_outcome_status") == "AVAILABLE"]
    activated = sorted(
        (row for row in available if row.get("runner_activated") is True),
        key=lambda row: str(row.get("runner_exit_timestamp") or ""),
    )
    deltas = []
    for row in activated:
        try:
            value = float(row.get("paired_delta_runner_minus_static_pct"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            deltas.append(value)
    count = len(deltas)
    mean_delta = round(sum(deltas) / count, 8) if count else None
    if count >= 2:
        midpoint = count // 2
        early = deltas[:midpoint]
        late = deltas[midpoint:]
        early_mean = sum(early) / len(early)
        late_mean = sum(late) / len(late)
        split_consistent = (early_mean > 0 and late_mean > 0) or (early_mean < 0 and late_mean < 0)
    else:
        early_mean = late_mean = None
        split_consistent = None

    if count:
        rng = random.Random(f"{MEASUREMENT_VERSION}:{count}:{','.join(map(str, deltas))}")
        observed = abs(mean_delta)
        more_extreme = 0
        for _ in range(max(1, int(permutations))):
            null_mean = sum(value if rng.random() < 0.5 else -value for value in deltas) / count
            more_extreme += abs(null_mean) >= observed
        null_p_value = round((more_extreme + 1) / (max(1, int(permutations)) + 1), 8)
    else:
        null_p_value = None

    if count < ACTIVATION_FLOOR:
        verdict = "UNDERPOWERED"
    elif count < VERDICT_FLOOR:
        verdict = "PRELIMINARY"
    elif mean_delta > 0 and null_p_value is not None and null_p_value <= 0.05 and split_consistent:
        verdict = "RUNNER_HELPS"
    else:
        verdict = "RUNNER_NEUTRAL_OR_HURTS"
    return {
        "measurement_version": MEASUREMENT_VERSION,
        "population_runner_eligible_positions": len(paired),
        "available_paired_outcomes": len(available),
        "no_activation_rows": sum(row.get("paired_outcome_tag") == "NO_ACTIVATION" for row in paired),
        "unavailable_rows": sum(row.get("paired_outcome_status") == "UNAVAILABLE" for row in paired),
        "activated_available_positions": count,
        "mean_paired_delta_runner_minus_static_pct": mean_delta,
        "paired_sign_flip_null_p_value": null_p_value,
        "null_p_value_formula": "(k+1)/(n+1)",
        "temporal_split_early_mean_delta": round(early_mean, 8) if early_mean is not None else None,
        "temporal_split_late_mean_delta": round(late_mean, 8) if late_mean is not None else None,
        "temporal_split_consistent_sign": split_consistent,
        "activation_floor": ACTIVATION_FLOOR,
        "verdict_floor": VERDICT_FLOOR,
        "verdict": verdict,
    }


def read_pool(path: Path = POOL_PATH) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    rows = []
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def analysis_report(path: Path = POOL_PATH) -> dict[str, Any]:
    return analyze_records(read_pool(path))
