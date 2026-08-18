"""Advisory-only discretionary override log.

This module records operator discretionary decisions into an append-only JSONL
pool. It has no broker, order, or Guard imports and does not alter precheck,
regime classification, admission, gate, or sizing logic.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import a2_measurement
import regime_layer_v0


POOL_NAME = "DISCRETIONARY_OVERRIDE_LOG"
SCHEMA_VERSION = "discretionary-override-log-v0"
DEFAULT_POOL_PATH = Path("v2_data") / "discretionary_override_log_v0.jsonl"
UNDERPOWERED_FLOOR = 50


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _append_jsonl_record(path: Path, record: dict[str, Any]) -> None:
    """Append and sync one complete JSONL row before returning."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "ab") as target:
            descriptor = -1
            fcntl.flock(target.fileno(), fcntl.LOCK_EX)
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _append_decision_once(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append one DECISION per decision_id under an exclusive file lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    decision_id = record["decision_id"]
    payload = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    with path.open("a+b", buffering=0) as target:
        fcntl.flock(target.fileno(), fcntl.LOCK_EX)
        target.seek(0)
        for raw_line in target:
            if not raw_line.strip():
                continue
            try:
                existing = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if (existing.get("record_type") == "DECISION"
                    and existing.get("decision_id") == decision_id):
                return existing
        target.seek(0, os.SEEK_END)
        target.write(payload)
        os.fsync(target.fileno())
    return record


def _normalize_precheck(precheck_result: dict[str, Any] | None) -> dict[str, Any]:
    if not precheck_result:
        return {
            "precheck_status": "MISSING",
            "precheck_version": None,
            "precheck_decision": None,
            "agreement_pct": None,
            "coverage_pct": None,
            "horizons": None,
        }
    return {
        "precheck_status": "FRESH",
        "precheck_version": precheck_result.get("version") or precheck_result.get("precheck_version"),
        "precheck_decision": precheck_result.get("decision"),
        "agreement_pct": precheck_result.get("agreement_pct"),
        "coverage_pct": precheck_result.get("input_coverage_pct") or precheck_result.get("coverage_pct"),
        "horizons": precheck_result.get("horizons"),
    }


def _normalize_regime(regime_tag: dict[str, Any] | None) -> dict[str, Any]:
    if not regime_tag:
        return {"regime_tag": "UNKNOWN", "regime_status": "UNKNOWN"}
    return {
        "regime_tag": regime_tag.get("state") or "UNKNOWN",
        "regime_status": regime_tag.get("status") or "UNKNOWN",
    }


def _operator_action(
    followed: bool,
    took: bool,
    precheck_status: str,
    precheck_decision: str | None,
) -> str:
    prefix = "FOLLOWED" if followed else "OVERRODE"
    if precheck_status == "MISSING" or precheck_decision not in {"YES", "NO"}:
        prefix = "NO_READ"
    suffix = "TOOK" if took else "SKIPPED"
    return f"{prefix}_{suffix}"


def _create_decision_record(
    *,
    symbol: str,
    trade_type: str,
    implied_direction: str,
    followed: bool,
    took: bool,
    decided_at: str | datetime | None = None,
    precheck_result: dict[str, Any] | None = None,
    regime_tag: dict[str, Any] | None = None,
    operator_rationale: str | None = None,
    decision_id: str | None = None,
    capture_origin: str = "DIRECT_API",
    operator_driven: bool = False,
    analysis_role: str = "PRIMARY",
) -> dict[str, Any]:
    """Build a DECISION row for immediate use by capture_decision_record."""
    precheck_block = _normalize_precheck(precheck_result)
    regime_block = _normalize_regime(regime_tag)
    decided_dt = _parse_dt(decided_at)
    record = {
        "schema_version": SCHEMA_VERSION,
        "pool": POOL_NAME,
        "record_type": "DECISION",
        "record_id": str(uuid.uuid4()),
        "supersedes_record_id": None,
        "advisory_only": True,
        "execution_authority": False,
        "part_of_frozen_cohort": False,
        "capture_origin": capture_origin,
        "operator_driven": bool(operator_driven),
        "analysis_role": analysis_role,
        "decision_id": decision_id or str(uuid.uuid4()),
        "decided_at": decided_dt.isoformat() if decided_dt else _utc_now(),
        "recorded_at": _utc_now(),
        "symbol": symbol,
        "trade_type": trade_type,
        "implied_direction": implied_direction,
        "implied_direction_sign": 1 if implied_direction == "bullish" else -1 if implied_direction == "bearish" else None,
        "operator_action": _operator_action(
            followed, took, precheck_block["precheck_status"],
            precheck_block["precheck_decision"],
        ),
        "followed_precheck": bool(followed),
        "took_trade": bool(took),
        "operator_rationale": operator_rationale,
        **precheck_block,
        **regime_block,
        "outcome_status": "UNAVAILABLE",
        "signed_return_60m": None,
        "signed_return_5m": None,
        "signed_return_15m": None,
        "signed_return_30m": None,
        "outcome_label_source": None,
        "outcome_labeled_at": None,
    }
    return record


def capture_decision_record(
    *,
    symbol: str,
    trade_type: str,
    implied_direction: str,
    followed: bool,
    took: bool,
    precheck_snapshot: dict[str, Any] | None,
    minute_bars: list[dict[str, Any]],
    now_minute: int,
    decided_at: str | datetime | None = None,
    operator_rationale: str | None = None,
    decision_id: str | None = None,
    capture_origin: str = "DIRECT_API",
    operator_driven: bool = False,
    analysis_role: str = "PRIMARY",
    pool_path: Path = DEFAULT_POOL_PATH,
) -> dict[str, Any]:
    """Capture and durably append one decision before any outcome is available."""
    regime_tag = regime_layer_v0.classify_regime(minute_bars=minute_bars, now_minute=now_minute)
    if precheck_snapshot and isinstance(precheck_snapshot.get("weighted_read"), dict):
        precheck_result = precheck_snapshot["weighted_read"]
    elif precheck_snapshot and ("version" in precheck_snapshot or "precheck_version" in precheck_snapshot):
        precheck_result = precheck_snapshot
    else:
        precheck_result = None
    record = _create_decision_record(
        symbol=symbol,
        trade_type=trade_type,
        implied_direction=implied_direction,
        followed=followed,
        took=took,
        decided_at=decided_at,
        precheck_result=precheck_result,
        regime_tag=regime_tag,
        operator_rationale=operator_rationale,
        decision_id=decision_id,
        capture_origin=capture_origin,
        operator_driven=operator_driven,
        analysis_role=analysis_role,
    )
    return _append_decision_once(pool_path, record)


def label_outcome(
    record: dict[str, Any],
    *,
    a2_episode: dict[str, Any] | None = None,
    session_quotes: list[dict[str, Any]] | None = None,
    pool_path: Path = DEFAULT_POOL_PATH,
) -> dict[str, Any]:
    """Append an OUTCOME_LABEL revision using a2_measurement; never impute."""
    if record.get("record_type") not in {"DECISION", "OUTCOME_LABEL"}:
        raise ValueError("outcome labels require a DECISION or OUTCOME_LABEL record")
    if not record.get("record_id") or not record.get("decision_id"):
        raise ValueError("outcome labels require record_id and decision_id linkage")

    updated = dict(record)
    updated["record_type"] = "OUTCOME_LABEL"
    updated["record_id"] = str(uuid.uuid4())
    updated["supersedes_record_id"] = record["record_id"]
    labeled_at = _utc_now()
    updated["recorded_at"] = labeled_at
    updated["outcome_labeled_at"] = labeled_at
    if not a2_episode:
        updated["outcome_status"] = "UNAVAILABLE"
        updated["signed_return_60m"] = None
        updated["signed_return_5m"] = None
        updated["signed_return_15m"] = None
        updated["signed_return_30m"] = None
        updated["outcome_label_source"] = None
    else:
        labeled = a2_measurement.label_episode(a2_episode, session_quotes=session_quotes)
        updated["outcome_status"] = labeled.get("label_status") or "UNAVAILABLE"
        updated["signed_return_60m"] = labeled.get("signed_return_60m")
        updated["signed_return_5m"] = labeled.get("signed_return_5m")
        updated["signed_return_15m"] = labeled.get("signed_return_15m")
        updated["signed_return_30m"] = labeled.get("signed_return_30m")
        updated["outcome_label_source"] = "a2_measurement.label_episode"
    _append_jsonl_record(pool_path, updated)
    return updated


def read_pool_records(*, pool_path: Path = DEFAULT_POOL_PATH) -> list[dict[str, Any]]:
    """Read valid JSONL records in durable append order."""
    pool_path = Path(pool_path)
    if not pool_path.exists():
        return []
    records = []
    with pool_path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {pool_path}:{line_number}") from exc
    return records


def latest_labeled_revisions(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the last appended OUTCOME_LABEL row for each decision_id."""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        decision_id = row.get("decision_id")
        if row.get("record_type") == "OUTCOME_LABEL" and decision_id:
            latest[decision_id] = row
    return latest


def read_latest_labeled_revisions(
    *, pool_path: Path = DEFAULT_POOL_PATH
) -> dict[str, dict[str, Any]]:
    return latest_labeled_revisions(read_pool_records(pool_path=pool_path))


def summarize_pool_counts(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Count-only power stub; this is not the pre-registered override analysis."""
    rows = list(rows)
    decision_count = sum(row.get("record_type") == "DECISION" for row in rows)
    override_count = sum(
        row.get("record_type") == "DECISION"
        and str(row.get("operator_action", "")).startswith("OVERRODE_")
        for row in rows
    )
    outcome_label_count = sum(row.get("record_type") == "OUTCOME_LABEL" for row in rows)
    return {
        "pool": POOL_NAME,
        "schema_version": SCHEMA_VERSION,
        "n_rows": len(rows),
        "n_decisions": decision_count,
        "n_override_decisions": override_count,
        "n_outcome_labels": outcome_label_count,
        "status": "UNDERPOWERED" if override_count < UNDERPOWERED_FLOOR else "COUNT_READY",
        "underpowered_floor": UNDERPOWERED_FLOOR,
    }
