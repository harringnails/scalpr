"""Typed, append-only Decision Replay research records.

This module is paper/shadow research infrastructure only.  It has no broker,
order, position, liquidation, server, or Guard imports.  Rejected decisions are
tracked in a physically separate hypothetical store and can never contribute to
portfolio P/L.  Evidence manifests bind a decision to the exact records that
were visible at decision time; verification never falls back to current data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

import entry_intelligence_v1 as intelligence
import feature_engine as fe


NO_TRADE_PLAN_VERSION = "no-trade-tracking-plan-v1"
GATE_RESULT_VERSION = "entry-intelligence-gate-result-v1"
EVIDENCE_SOURCE_VERSION = "entry-intelligence-evidence-source-v1"
EVIDENCE_MANIFEST_VERSION = "entry-intelligence-evidence-manifest-v1"
HYPOTHETICAL_OUTCOME_VERSION = "entry-intelligence-hypothetical-outcome-v1"
DECISION_EVENT_VERSION = "entry-intelligence-decision-event-v1"

DataState = Literal["FRESH", "STALE", "MISSING", "UNAVAILABLE", "UNUSABLE"]
GateState = Literal[
    "PASS", "FAIL", "UNAVAILABLE", "NOT_EVALUATED", "NOT_APPLICABLE"]


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed.astimezone(timezone.utc) if parsed.tzinfo else
            parsed.replace(tzinfo=timezone.utc))


def _append_unique(record: dict, path: Path | str, identity_key: str) -> bool:
    target = Path(path)
    identity = record.get(identity_key)
    if not identity:
        raise ValueError(f"{identity_key} required")
    if any(row.get(identity_key) == identity for row in fe._iter_jsonl(target)):
        return False
    return fe._atomic_append(target, record)


def _json_safe(value: Any) -> Any:
    """Canonical JSON value used identically for storage and content hashing."""
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


class NoTradeTrackingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["no-trade-tracking-plan-v1"] = NO_TRADE_PLAN_VERSION
    plan_id: str
    decision_id: str
    cohort_id: str
    episode_key: str
    side: Literal["CALL", "PUT"]
    status: Literal["TRACKABLE", "UNTRACKABLE"]
    reason_code: str
    config_version: str
    config_hash: str
    created_at: datetime
    selected_contract: Optional[dict[str, Any]] = None
    entry_quote_observed_at: Optional[datetime] = None
    entry_ask: Optional[float] = None
    stop_bid: Optional[float] = None
    target_bid: Optional[float] = None
    outcome_horizon_minutes: int = Field(gt=0)
    bid_poll_interval_seconds: float = Field(gt=0)
    max_fresh_gap_seconds: float = Field(gt=0)
    minimum_coverage_fraction: float = Field(gt=0, le=1)
    return_horizons_minutes: list[int]
    cost_model: dict[str, Any]
    realized_trade: Literal[False] = False
    portfolio_pnl_eligible: Literal[False] = False
    execution_authority: Literal[False] = False

    @model_validator(mode="after")
    def tracking_shape(self):
        frozen = (
            self.selected_contract, self.entry_quote_observed_at, self.entry_ask,
            self.stop_bid, self.target_bid,
        )
        if self.status == "TRACKABLE" and not all(value is not None for value in frozen):
            raise ValueError("TRACKABLE plan requires a frozen contract and bid geometry")
        if self.status == "UNTRACKABLE" and any(value is not None for value in frozen):
            raise ValueError("UNTRACKABLE plan cannot carry reconstructed contract data")
        return self


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["entry-intelligence-gate-result-v1"] = GATE_RESULT_VERSION
    gate_result_id: str
    decision_id: str
    cohort_id: str
    gate_name: str
    gate_version: str
    gate_group: Literal["DIRECTION", "EXECUTION"]
    result: GateState
    reason_code: str
    reason_text: Optional[str] = None
    source_data_state: DataState
    comparator: str
    threshold: Optional[Any] = None
    observed_value: Optional[Any] = None
    units: str
    threshold_or_config_hash: str
    source_ref: str
    blocking: bool
    evaluation_order: int = Field(ge=0)
    evaluated_at: datetime
    rules_hash: str
    config_version: str
    config_hash: str
    execution_authority: Literal[False] = False


class BoundedReplayPersistence:
    """Bounded daemon writer so replay fsync never enters Guard/order paths."""

    def __init__(self, max_pending: int = 256):
        self._queue: Queue = Queue(maxsize=max(1, int(max_pending)))
        self._stop = Event()
        self._lock = Lock()
        self._failure: Optional[BaseException] = None
        self._thread = Thread(
            target=self._run, name="scalpr-replay-persistence", daemon=True)
        self._thread.start()

    def submit(self, callback, *args, **kwargs) -> None:
        self.raise_if_failed()
        try:
            self._queue.put_nowait((callback, args, kwargs))
        except Full as exc:
            raise RuntimeError("replay persistence queue is full") from exc

    @property
    def pending(self) -> int:
        return int(self._queue.unfinished_tasks)

    def _run(self) -> None:
        while not self._stop.is_set() or self._queue.unfinished_tasks:
            try:
                callback, args, kwargs = self._queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                callback(*args, **kwargs)
            except BaseException as exc:  # surfaced to collector health on next tick
                with self._lock:
                    if self._failure is None:
                        self._failure = exc
            finally:
                self._queue.task_done()

    def raise_if_failed(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise RuntimeError(
                f"replay persistence failed: {type(failure).__name__}: {failure}")

    def flush(self, timeout_seconds: float = 5.0) -> None:
        deadline = time.monotonic() + float(timeout_seconds)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        if self._queue.unfinished_tasks:
            raise TimeoutError("replay persistence queue did not drain")
        self.raise_if_failed()

    def close(self, timeout_seconds: float = 5.0) -> None:
        failure = None
        try:
            self.flush(timeout_seconds)
        except BaseException as exc:
            failure = exc
        finally:
            self._stop.set()
            self._thread.join(timeout=float(timeout_seconds))
        if failure is not None:
            raise failure


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: str
    schema_version: str
    record_id_or_location: str
    content_hash: str
    observed_at: datetime
    received_at: datetime
    data_state: DataState
    retention_guarantee: str


class EvidenceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "entry-intelligence-evidence-manifest-v1"] = EVIDENCE_MANIFEST_VERSION
    manifest_id: str
    decision_id: str
    decision_content_hash: str
    cohort_id: str
    config_version: str
    config_hash: str
    created_at: datetime
    evidence: list[EvidenceReference]
    verification_policy: Literal[
        "LOAD_REFERENCED_RECORD_AND_VERIFY_HASH_NO_CURRENT_FALLBACK"] = (
            "LOAD_REFERENCED_RECORD_AND_VERIFY_HASH_NO_CURRENT_FALLBACK")
    execution_authority: Literal[False] = False


class DecisionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "entry-intelligence-decision-event-v1"] = DECISION_EVENT_VERSION
    decision_event_id: str
    decision_id: str
    cohort_id: str
    event_type: Literal["DECISION_EVIDENCE_FROZEN"] = "DECISION_EVIDENCE_FROZEN"
    decision_record_ref: str
    manifest_id: str
    gate_result_ids: list[str]
    episode_record_id: Optional[str] = None
    no_trade_tracking_plan_id: Optional[str] = None
    outcome_store: Literal[
        "QUALIFIED_DECISION_SHADOW", "HYPOTHETICAL_NO_TRADE", "NONE"]
    emitted_at: datetime
    config_version: str
    config_hash: str
    execution_authority: Literal[False] = False


def build_no_trade_tracking_plan(*, packet: dict, side: str, selection: dict,
                                 config: dict, created_at: Any) -> NoTradeTrackingPlan:
    """Freeze a rejected decision's counterfactual contract, or mark it untrackable.

    This function must only be called after the shared episode ledger admits the
    rejection candidate.  Contract selection is point-in-time; absent data is
    never reconstructed later.
    """
    if packet.get("decision") != "NO_TRADE":
        raise ValueError("NoTradeTrackingPlan requires a NO_TRADE packet")
    selected = selection.get("selected")
    outcome = config["outcome"]
    base = {
        "decision_id": packet["decision_id"], "cohort_id": packet["cohort_id"],
        "episode_key": packet["episode_key"], "side": side,
        "config_version": packet["config_version"],
        "config_hash": packet["config_hash"], "created_at": _utc(created_at),
        "outcome_horizon_minutes": int(config["episode"]["outcome_horizon_minutes"]),
        "bid_poll_interval_seconds": float(outcome["bid_poll_interval_seconds"]),
        "max_fresh_gap_seconds": float(outcome["max_fresh_gap_seconds"]),
        "minimum_coverage_fraction": float(outcome["minimum_coverage_fraction"]),
        "return_horizons_minutes": list(outcome["return_horizons_minutes"]),
        "cost_model": dict(outcome["cost_model"]),
    }
    if selected:
        stop, target = intelligence.option_bid_levels(
            float(selected["ask"]),
            risk_fraction=float(outcome["option_risk_fraction"]),
            reward_risk=float(outcome["reward_risk"]),
        )
        contract = dict(selected)
        observed = _utc(contract["quote_observed_at"])
        identity = {
            "version": NO_TRADE_PLAN_VERSION, "decision_id": packet["decision_id"],
            "option_symbol": contract.get("option_symbol"),
            "entry_quote_observed_at": observed.isoformat(),
            "config_hash": packet["config_hash"],
        }
        return NoTradeTrackingPlan(
            **base, plan_id=fe.canonical_hash(identity), status="TRACKABLE",
            reason_code="COUNTERFACTUAL_CONTRACT_FROZEN_AT_DECISION",
            selected_contract=contract, entry_quote_observed_at=observed,
            entry_ask=float(contract["ask"]), stop_bid=stop, target_bid=target)
    reason = str(selection.get("reason") or "NO_POINT_IN_TIME_CONTRACT")
    identity = {
        "version": NO_TRADE_PLAN_VERSION, "decision_id": packet["decision_id"],
        "status": "UNTRACKABLE", "reason": reason,
        "config_hash": packet["config_hash"],
    }
    return NoTradeTrackingPlan(
        **base, plan_id=fe.canonical_hash(identity), status="UNTRACKABLE",
        reason_code=reason.upper())


def append_no_trade_plan(plan: NoTradeTrackingPlan | dict,
                         path: Path | str) -> bool:
    payload = (plan.model_dump(mode="json") if isinstance(plan, BaseModel)
               else dict(plan))
    return _append_unique(payload, path, "plan_id")


def _gate(*, packet: dict, name: str, group: str, result: GateState,
          reason_code: str, source_data_state: DataState, comparator: str,
          threshold: Any, observed: Any, units: str, blocking: bool,
          order: int, evaluated_at: Any) -> GateResult:
    gate_version = (packet.get("rules_version", "entry-reversal-rules-v1-draft")
                    if group == "DIRECTION" else "entry-execution-gates-v1")
    threshold_hash = fe.canonical_hash({
        "gate_version": gate_version, "gate_name": name,
        "threshold": _json_safe(threshold), "config_hash": packet["config_hash"],
    })
    identity = {
        "version": GATE_RESULT_VERSION, "decision_id": packet["decision_id"],
        "gate_name": name, "gate_version": gate_version,
        "result": result, "reason_code": reason_code,
        "evaluation_order": order, "rules_hash": packet["rules_hash"],
        "config_hash": packet["config_hash"],
    }
    return GateResult(
        gate_result_id=fe.canonical_hash(identity), decision_id=packet["decision_id"],
        cohort_id=packet["cohort_id"], gate_name=name,
        gate_version=gate_version, gate_group=group,
        result=result, reason_code=reason_code, source_data_state=source_data_state,
        comparator=comparator, threshold=threshold, observed_value=observed,
        units=units, threshold_or_config_hash=threshold_hash,
        source_ref=(
            f"evidence-manifest:{packet['decision_id']}#"
            f"{'TECHNICAL_SETUP' if group == 'DIRECTION' else 'CONTRACT_SELECTION'}"),
        blocking=blocking, evaluation_order=order, evaluated_at=_utc(evaluated_at),
        rules_hash=packet["rules_hash"], config_version=packet["config_version"],
        config_hash=packet["config_hash"])


def build_gate_results(*, packet: dict, setup: dict, selection: dict,
                       config: dict, evaluated_at: Any) -> list[GateResult]:
    """Materialize gate states without collapsing unavailable into failure."""
    results: list[GateResult] = []
    setup_state = str(setup.get("status") or "MISSING")
    if setup_state not in {"FRESH", "STALE", "MISSING", "UNAVAILABLE", "UNUSABLE"}:
        setup_state = "UNUSABLE"
    passed, failed = set(setup.get("passed") or []), set(setup.get("failed") or [])
    for order, name in enumerate((
            "atr_extension", "level_proximity", "momentum_slowing",
            "causal_confirmation"), start=1):
        if setup_state != "FRESH":
            state, reason = "UNAVAILABLE", f"SOURCE_{setup_state}"
            observed = None
        elif name in passed:
            state, reason, observed = "PASS", "GATE_PASSED", True
        elif name in failed:
            state, reason, observed = "FAIL", "GATE_FAILED", False
        else:
            state, reason, observed = "NOT_EVALUATED", "GATE_NOT_EVALUATED", None
        results.append(_gate(
            packet=packet, name=name, group="DIRECTION", result=state,
            reason_code=reason, source_data_state=setup_state,
            comparator="IS_TRUE", threshold=True, observed=observed,
            units="boolean", blocking=True, order=order,
            evaluated_at=evaluated_at))

    selection_state = str(selection.get("source_status") or "MISSING")
    if selection_state not in {"FRESH", "STALE", "MISSING", "UNAVAILABLE", "UNUSABLE"}:
        selection_state = "UNUSABLE"
    reason = str(selection.get("reason") or "")
    if reason.startswith("execution_not_evaluated"):
        state, reason_code = "NOT_EVALUATED", reason.upper()
    elif selection_state != "FRESH":
        state, reason_code = "UNAVAILABLE", f"SOURCE_{selection_state}"
    elif selection.get("selected"):
        state, reason_code = "PASS", "CONTRACT_SELECTED"
    else:
        state, reason_code = "FAIL", "NO_CONTRACT_PASSED_EXECUTION_GATES"
    results.append(_gate(
        packet=packet, name="contract_selection", group="EXECUTION", result=state,
        reason_code=reason_code, source_data_state=selection_state,
        comparator="ALL_APPROVED_EXECUTION_GATES_PASS",
        threshold={
            key: config["execution"][key] for key in (
                "max_quote_age_seconds", "max_spread_pct", "min_contract_volume",
                "min_open_interest", "delta_min", "delta_max", "max_ask_price",
                "dte_min", "dte_max")
        },
        observed=(selection.get("selected") or None), units="mixed",
        blocking=True, order=5, evaluated_at=evaluated_at))
    return results


def append_gate_results(results: list[GateResult], path: Path | str) -> int:
    return sum(_append_unique(result.model_dump(mode="json"), path,
                              "gate_result_id") for result in results)


def _source_record(*, decision_id: str, source_type: str, content: dict,
                   observed_at: Any, received_at: Any, data_state: DataState,
                   location: str, config_version: str,
                   config_hash: str) -> tuple[dict, EvidenceReference]:
    content = _json_safe(content)
    content_hash = fe.canonical_hash(content)
    record_id = fe.canonical_hash({
        "version": EVIDENCE_SOURCE_VERSION, "decision_id": decision_id,
        "source_type": source_type, "content_hash": content_hash,
    })
    record = {
        "schema_version": EVIDENCE_SOURCE_VERSION, "source_record_id": record_id,
        "decision_id": decision_id, "source_type": source_type,
        "config_version": config_version, "config_hash": config_hash,
        "content_hash": content_hash, "content": content,
        "observed_at": _utc(observed_at).isoformat(),
        "received_at": _utc(received_at).isoformat(), "data_state": data_state,
        "execution_authority": False,
    }
    reference = EvidenceReference(
        source_type=source_type, schema_version=EVIDENCE_SOURCE_VERSION,
        record_id_or_location=f"{location}#{record_id}", content_hash=content_hash,
        observed_at=_utc(observed_at), received_at=_utc(received_at),
        data_state=data_state,
        retention_guarantee="retain_complete_sealed_partition_for_cohort_lifetime")
    return record, reference


def write_evidence_manifest(*, packet: dict, setup: dict, selection: dict,
                            config: dict, source_path: Path | str,
                            manifest_path: Path | str,
                            created_at: Any) -> EvidenceManifest:
    """Persist and bind only the evidence actually consumed by this decision."""
    source_target = Path(source_path)
    location = source_target.name
    sources = (
        ("TECHNICAL_SETUP", dict(setup), str(setup.get("status") or "MISSING"),
         packet["observed_at"]),
        ("CONTRACT_SELECTION", dict(selection),
         str(selection.get("source_status") or "MISSING"),
         ((selection.get("selected") or {}).get("quote_observed_at")
          or packet["observed_at"])),
        ("CONFIG_SNAPSHOT", dict(config), "FRESH", packet["observed_at"]),
    )
    references = []
    for source_type, content, state, source_observed_at in sources:
        if state not in {"FRESH", "STALE", "MISSING", "UNAVAILABLE", "UNUSABLE"}:
            state = "UNUSABLE"
        record, reference = _source_record(
            decision_id=packet["decision_id"], source_type=source_type,
            content=content, observed_at=source_observed_at,
            received_at=packet["decided_at"], data_state=state, location=location,
            config_version=packet["config_version"],
            config_hash=packet["config_hash"])
        _append_unique(record, source_target, "source_record_id")
        references.append(reference)
    decision_content_hash = fe.canonical_hash(_json_safe(packet))
    identity = {
        "version": EVIDENCE_MANIFEST_VERSION, "decision_id": packet["decision_id"],
        "decision_content_hash": decision_content_hash,
        "references": [ref.model_dump(mode="json") for ref in references],
        "config_hash": packet["config_hash"],
    }
    manifest = EvidenceManifest(
        manifest_id=fe.canonical_hash(identity), decision_id=packet["decision_id"],
        decision_content_hash=decision_content_hash,
        cohort_id=packet["cohort_id"], config_version=packet["config_version"],
        config_hash=packet["config_hash"], created_at=_utc(created_at),
        evidence=references)
    _append_unique(manifest.model_dump(mode="json"), manifest_path, "manifest_id")
    return manifest


def verify_evidence_manifest(manifest: EvidenceManifest | dict,
                             source_path: Path | str,
                             decision_packet: Optional[dict] = None) -> dict:
    """Verify retrieval and content hashes; never substitute current evidence."""
    parsed = (manifest if isinstance(manifest, EvidenceManifest)
              else EvidenceManifest.model_validate(manifest))
    rows = {row.get("source_record_id"): row for row in fe._iter_jsonl(source_path)}
    failures = []
    if (decision_packet is not None
            and fe.canonical_hash(_json_safe(decision_packet))
            != parsed.decision_content_hash):
        failures.append({
            "record_id": parsed.decision_id,
            "reason": "DECISION_CONTENT_HASH_MISMATCH"})
    for reference in parsed.evidence:
        record_id = reference.record_id_or_location.rsplit("#", 1)[-1]
        record = rows.get(record_id)
        if not record:
            failures.append({"record_id": record_id, "reason": "RECORD_MISSING"})
            continue
        if fe.canonical_hash(record.get("content")) != reference.content_hash:
            failures.append({"record_id": record_id, "reason": "CONTENT_HASH_MISMATCH"})
    return {
        "manifest_id": parsed.manifest_id,
        "status": "VERIFIED" if not failures else "UNRECOVERABLE",
        "failures": failures,
        "current_data_fallback_used": False,
    }


def persist_decision_bundle(*, packet: dict, setup: dict, selection: dict,
                            config: dict, gate_results: list[GateResult],
                            decision_path: Path | str,
                            gate_result_path: Path | str,
                            evidence_source_path: Path | str,
                            evidence_manifest_path: Path | str,
                            decision_event_path: Path | str,
                            no_trade_plan_path: Path | str,
                            episode_record_id: Optional[str],
                            no_trade_plan: Optional[NoTradeTrackingPlan | dict],
                            created_at: Any) -> None:
    """Write one ordered replay bundle from the bounded persistence worker."""
    append_gate_results(gate_results, gate_result_path)
    manifest = write_evidence_manifest(
        packet=packet, setup=setup, selection=selection, config=config,
        source_path=evidence_source_path, manifest_path=evidence_manifest_path,
        created_at=created_at)
    plan_payload = None
    if no_trade_plan is not None:
        plan_payload = (no_trade_plan.model_dump(mode="json")
                        if isinstance(no_trade_plan, BaseModel)
                        else dict(no_trade_plan))
        append_no_trade_plan(plan_payload, no_trade_plan_path)
    if packet.get("decision") in {"CALL", "PUT"}:
        outcome_store = "QUALIFIED_DECISION_SHADOW"
    elif plan_payload and plan_payload.get("status") == "TRACKABLE":
        outcome_store = "HYPOTHETICAL_NO_TRADE"
    else:
        outcome_store = "NONE"
    identity = {
        "version": DECISION_EVENT_VERSION, "decision_id": packet["decision_id"],
        "manifest_id": manifest.manifest_id,
        "gate_result_ids": [result.gate_result_id for result in gate_results],
        "episode_record_id": episode_record_id,
        "no_trade_tracking_plan_id": (
            plan_payload.get("plan_id") if plan_payload else None),
        "config_hash": packet["config_hash"],
    }
    event = DecisionEvent(
        decision_event_id=fe.canonical_hash(identity),
        decision_id=packet["decision_id"], cohort_id=packet["cohort_id"],
        decision_record_ref=f"{Path(decision_path).name}#{packet['decision_id']}",
        manifest_id=manifest.manifest_id,
        gate_result_ids=[result.gate_result_id for result in gate_results],
        episode_record_id=episode_record_id,
        no_trade_tracking_plan_id=(
            plan_payload.get("plan_id") if plan_payload else None),
        outcome_store=outcome_store, emitted_at=_utc(created_at),
        config_version=packet["config_version"], config_hash=packet["config_hash"])
    _append_unique(event.model_dump(mode="json"), decision_event_path,
                   "decision_event_id")


def tracking_decision_from_plan(plan: NoTradeTrackingPlan | dict) -> dict:
    parsed = (plan if isinstance(plan, NoTradeTrackingPlan)
              else NoTradeTrackingPlan.model_validate(plan))
    if parsed.status != "TRACKABLE":
        raise ValueError("only TRACKABLE plans have a bid path")
    contract = parsed.selected_contract or {}
    return {
        "config_version": parsed.config_version, "config_hash": parsed.config_hash,
        "decision_id": parsed.decision_id, "cohort_id": parsed.cohort_id,
        "option_symbol": contract["option_symbol"],
        "entry_quote_observed_at": parsed.entry_quote_observed_at.isoformat(),
        "entry_ask": parsed.entry_ask, "target_bid": parsed.target_bid,
        "stop_bid": parsed.stop_bid,
        "outcome_horizon_minutes": parsed.outcome_horizon_minutes,
        "bid_poll_interval_seconds": parsed.bid_poll_interval_seconds,
        "max_fresh_gap_seconds": parsed.max_fresh_gap_seconds,
        "minimum_coverage_fraction": parsed.minimum_coverage_fraction,
        "return_horizons_minutes": parsed.return_horizons_minutes,
        "cost_model": parsed.cost_model,
        "no_trade_tracking_plan_id": parsed.plan_id,
    }


def decorate_hypothetical_outcome(outcome: dict,
                                  plan: NoTradeTrackingPlan | dict) -> dict:
    parsed = (plan if isinstance(plan, NoTradeTrackingPlan)
              else NoTradeTrackingPlan.model_validate(plan))
    payload = {
        **outcome,
        "schema_version": HYPOTHETICAL_OUTCOME_VERSION,
        "outcome_kind": "HYPOTHETICAL_NO_TRADE",
        "no_trade_tracking_plan_id": parsed.plan_id,
        "realized_trade": False,
        "portfolio_pnl_eligible": False,
        "execution_authority": False,
    }
    payload["hypothetical_outcome_id"] = fe.canonical_hash({
        "version": HYPOTHETICAL_OUTCOME_VERSION,
        "decision_id": payload.get("decision_id"),
        "status": payload.get("status"),
        "evaluated_at": payload.get("evaluated_at"),
        "tracking_plan_id": parsed.plan_id,
    })
    return payload


def append_hypothetical_outcome(record: dict, path: Path | str) -> bool:
    return _append_unique(record, path, "hypothetical_outcome_id")
