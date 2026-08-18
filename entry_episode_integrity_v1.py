"""Fail-closed episode quarantine and cross-side integrity controls."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import feature_engine as fe


QUARANTINE_SCHEMA_VERSION = "entry-intelligence-episode-quarantine-v1"
QUARANTINE_MANIFEST_VERSION = "entry-intelligence-episode-quarantine-manifest-v1"
INTEGRITY_EVENT_VERSION = "entry-intelligence-integrity-event-v1"
QUARANTINED_PREFIX_ADMISSION = "QUARANTINED_PREFIX_ADMISSION"
DOUBLE_QUALIFICATION = "INTEGRITY_CROSS_SIDE_DOUBLE_QUALIFICATION"
DEFAULT_QUARANTINE_MANIFEST = Path("entry_intelligence_episode_quarantine_v1.jsonl")
DEFAULT_INTEGRITY_EVENT_LOG = Path("entry_intelligence_integrity_events_v1.jsonl")


class CrossSideDoubleQualificationError(RuntimeError):
    """Raised after both conflicting candidates are durably quarantined."""


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def has_qualified_reversal(setup: dict) -> bool:
    return (
        setup.get("status") == "FRESH"
        and bool(setup.get("qualified"))
        and setup.get("reference_extreme_bucket") is not None
    )


def _quarantine_record(
    candidate: dict,
    *,
    quarantine_status: str,
    detected_at: datetime,
    source_collector_version: str,
    setup_qualified: bool | None,
    additional_exclusion_reasons: Iterable[str] = (),
) -> dict:
    reasons = [quarantine_status, *additional_exclusion_reasons]
    identity = {
        "schema_version": QUARANTINE_SCHEMA_VERSION,
        "quarantine_status": quarantine_status,
        "episode_record_id": candidate.get("episode_record_id"),
        "episode_key": candidate.get("episode_key"),
        "decision_id": candidate.get("decision_id"),
        "decided_at": candidate.get("decided_at"),
    }
    return {
        "schema_version": QUARANTINE_SCHEMA_VERSION,
        "record_type": "QUARANTINE_ENTRY",
        "quarantine_record_id": fe.canonical_hash(identity),
        "quarantine_status": quarantine_status,
        "exclusion_reasons": reasons,
        "episode_record_id": candidate.get("episode_record_id"),
        "episode_key": candidate.get("episode_key"),
        "decision_id": candidate.get("decision_id"),
        "cohort_id": candidate.get("cohort_id"),
        "symbol": candidate.get("symbol"),
        "side": candidate.get("side"),
        "session_date": candidate.get("session_date"),
        "decided_at": candidate.get("decided_at"),
        "original_admitted": candidate.get("admitted"),
        "setup_qualified": setup_qualified,
        "source_collector_version": source_collector_version,
        "detected_at": _utc(detected_at).isoformat(),
        "eligible_for_a2_measurement": False,
        "eligible_for_phase4_inference": False,
        "eligible_for_cohort_reuse": False,
        "manual_review_required": quarantine_status == DOUBLE_QUALIFICATION,
        "execution_authority": False,
    }


def append_quarantine_records(records: Iterable[dict], path: Path | str) -> int:
    path = Path(path)
    existing = {
        row.get("quarantine_record_id") for row in fe._iter_jsonl(path)
        if row.get("quarantine_record_id")
    }
    appended = 0
    for record in records:
        record_id = record.get("quarantine_record_id")
        if not record_id or record_id in existing:
            continue
        fe._atomic_append(path, record)
        existing.add(record_id)
        appended += 1
    return appended


def quarantined_episode_record_ids(path: Path | str) -> set[str]:
    return {
        str(row.get("episode_record_id")) for row in fe._iter_jsonl(path)
        if row.get("record_type") == "QUARANTINE_ENTRY" and row.get("episode_record_id")
    }


def quarantine_cross_side_double_qualification(
    candidates: Iterable[dict],
    *,
    quarantine_path: Path | str,
    integrity_event_path: Path | str,
    detected_at: datetime,
    source_collector_version: str,
) -> None:
    candidates = list(candidates)
    sides = {str(candidate.get("side") or "").upper() for candidate in candidates}
    if sides != {"CALL", "PUT"}:
        return
    records = [
        _quarantine_record(
            candidate,
            quarantine_status=DOUBLE_QUALIFICATION,
            detected_at=detected_at,
            source_collector_version=source_collector_version,
            setup_qualified=True,
        )
        for candidate in candidates
    ]
    append_quarantine_records(records, quarantine_path)
    event_identity = {
        "event_type": DOUBLE_QUALIFICATION,
        "decided_at": _utc(detected_at).isoformat(),
        "episode_keys": sorted(str(candidate.get("episode_key")) for candidate in candidates),
    }
    event = {
        "schema_version": INTEGRITY_EVENT_VERSION,
        "integrity_event_id": fe.canonical_hash(event_identity),
        "event_type": DOUBLE_QUALIFICATION,
        "severity": "FAIL_CLOSED",
        "detected_at": _utc(detected_at).isoformat(),
        "source_collector_version": source_collector_version,
        "sides": sorted(sides),
        "episode_keys": event_identity["episode_keys"],
        "quarantine_record_ids": sorted(record["quarantine_record_id"] for record in records),
        "manual_review_required": True,
        "execution_authority": False,
    }
    existing_events = {
        row.get("integrity_event_id") for row in fe._iter_jsonl(integrity_event_path)
        if row.get("integrity_event_id")
    }
    if event["integrity_event_id"] not in existing_events:
        fe._atomic_append(integrity_event_path, event)
    raise CrossSideDoubleQualificationError(DOUBLE_QUALIFICATION)


def write_prefix_quarantine_manifest(
    *,
    episodes_path: Path | str,
    evidence_sources_path: Path | str,
    manifest_path: Path | str,
    cutoff: datetime,
    generated_at: datetime,
    source_collector_version: str,
) -> dict:
    """Write a point-in-time manifest without altering the source episode ledger."""
    episodes_path = Path(episodes_path)
    evidence_sources_path = Path(evidence_sources_path)
    manifest_path = Path(manifest_path)
    cutoff = _utc(cutoff)
    source_bytes = episodes_path.read_bytes()
    admitted = [
        row for line in source_bytes.decode("utf-8").splitlines()
        if line.strip()
        for row in [json.loads(line)]
        if row.get("admitted") is True and _utc(row["decided_at"]) <= cutoff
    ]
    decision_ids = {row.get("decision_id") for row in admitted}
    setup_qualified: dict[str, bool | None] = {}
    for row in fe._iter_jsonl(evidence_sources_path):
        if row.get("source_type") != "TECHNICAL_SETUP" or row.get("decision_id") not in decision_ids:
            continue
        setup_qualified[str(row.get("decision_id"))] = (row.get("content") or {}).get("qualified")

    entries = []
    for episode in admitted:
        qualified = setup_qualified.get(str(episode.get("decision_id")))
        additional = ["PREFIX_PRODUCED_BY_COLLECTOR_V1_1"]
        if qualified is True:
            additional.append("QUALIFIED_BUT_PRE_FIX_AND_HISTORICAL_UNPROVEN_PROVENANCE")
        entries.append(_quarantine_record(
            episode,
            quarantine_status=QUARANTINED_PREFIX_ADMISSION,
            detected_at=generated_at,
            source_collector_version=source_collector_version,
            setup_qualified=qualified,
            additional_exclusion_reasons=additional,
        ))
    entries.sort(key=lambda row: (str(row.get("decided_at")), str(row.get("side"))))

    sides_by_timestamp: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        sides_by_timestamp[str(entry.get("decided_at"))].add(str(entry.get("side")))
    collision_groups = sum(sides == {"CALL", "PUT"} for sides in sides_by_timestamp.values())
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    entries_hash = fe.canonical_hash(entries)
    header = {
        "schema_version": QUARANTINE_MANIFEST_VERSION,
        "record_type": "MANIFEST_HEADER",
        "quarantine_status": QUARANTINED_PREFIX_ADMISSION,
        "generated_at": _utc(generated_at).isoformat(),
        "prefix_cutoff_decided_at": cutoff.isoformat(),
        "source_collector_version": source_collector_version,
        "source_episode_log": episodes_path.name,
        "source_episode_log_sha256": source_hash,
        "manifest_entries_hash": entries_hash,
        "n_admissions_quarantined": len(entries),
        "n_cross_side_collision_groups": collision_groups,
        "n_qualified_but_dually_ineligible": sum(row.get("setup_qualified") is True for row in entries),
        "preservation_rule": "SOURCE_EPISODE_LEDGER_UNCHANGED",
        "exclusion_rule": "MATCH_EPISODE_RECORD_ID",
        "execution_authority": False,
    }
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    body = "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":"))
                       for row in [header, *entries]) + "\n"
    temporary.write_text(body, encoding="utf-8")
    temporary.replace(manifest_path)
    return header
