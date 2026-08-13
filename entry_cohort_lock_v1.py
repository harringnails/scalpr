"""Append-only pre-open lock registry for Entry Intelligence draft cohorts."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import entry_contract_data_v1 as contract_data
import entry_intelligence_v1 as intelligence
import feature_engine as fe
import scalpr_config


LOCK_RECORD_VERSION = "entry-intelligence-cohort-lock-v1"
ACCOUNT_FLAT_PROOF_VERSION = "scalpr-account-flat-proof-v1"
ACCOUNT_FLAT_PROOF_MAX_AGE_SECONDS = 5.0
MARKET_CALENDAR_PROOF_VERSION = "scalpr-market-calendar-proof-v1"
MARKET_CALENDAR_PROOF_MAX_AGE_SECONDS = 5.0
FEED_SANITY_EVIDENCE_VERSION = "entry-intelligence-manual-feed-sanity-v1"
DISK_HEADROOM_MIN_FRACTION = 0.15
DEFAULT_LOCK_REGISTRY = Path("entry_intelligence_cohort_locks_v1.jsonl")
ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent

IMPLEMENTATION_FILES = {
    "decision_packet_engine_hash": "entry_intelligence_v1.py",
    "bid_capture_hash": "entry_bid_capture_v1.py",
    "outcome_engine_hash": "entry_bid_capture_v1.py",
    "episode_engine_hash": "entry_episode_research_v1.py",
    "decision_replay_hash": "decision_replay_v1.py",
    "collector_hash": "entry_bid_collector_v1.py",
    "contract_assembly_engine_hash": "entry_contract_data_v1.py",
    "config_file_sha256": "config/scalpr.yaml",
}


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _walk_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, str):
        yield value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def expected_implementation_hashes(document: dict, *, root=ROOT) -> dict:
    """Recompute every cohort-level implementation/content hash from source."""
    proposed = document.get("proposed") or {}
    rules = proposed.get("direction_rules")
    execution = proposed.get("execution")
    if not isinstance(rules, dict) or not isinstance(execution, dict):
        raise ValueError("cohort rules/execution blocks are missing")
    root = Path(root)
    config = scalpr_config.load_entry_intelligence_config(
        root / "config/scalpr.yaml")
    assembly_hash = contract_data.contract_assembly_hash(config)
    expected = {
        "rules_hash": intelligence.rules_hash(rules),
        "contract_assembly_hash": assembly_hash,
        # This is the frozen cohort-level selection policy. Decision-time
        # selection records additionally bind their observed assembly universe.
        "contract_selection_hash": fe.canonical_hash({
            "contract_selection_version": intelligence.CONTRACT_SELECTION_VERSION,
            "execution_rules": execution,
            "contract_assembly_hash": assembly_hash,
        }),
        "config_hash": config["config_hash"],
    }
    for field, relative in IMPLEMENTATION_FILES.items():
        expected[field] = _sha256_file(root / relative)
    return expected


def validate_implementation_hashes(document: dict, *, root=ROOT) -> list[str]:
    provided = ((document.get("proposed") or {}).get("implementation_hashes")
                or {})
    if not isinstance(provided, dict):
        return ["implementation_hashes_missing"]
    try:
        expected = expected_implementation_hashes(document, root=root)
    except Exception:
        return ["implementation_hash_recompute_failed"]
    problems = []
    for field, actual in expected.items():
        stamped = str(provided.get(field) or "")
        if not re.fullmatch(r"[0-9a-f]{64}", stamped):
            problems.append(f"{field}_missing_or_invalid")
        elif stamped != actual:
            problems.append(f"{field}_mismatch")
    for field in sorted(set(provided) - set(expected)):
        problems.append(f"{field}_unverifiable")
    return problems


def disk_headroom_proof(*, root=ROOT, disk_usage_fn=shutil.disk_usage) -> dict:
    usage = disk_usage_fn(Path(root))
    total, free = int(usage.total), int(usage.free)
    if total <= 0:
        raise ValueError("disk total is unavailable")
    fraction = free / total
    return {
        "schema_version": "scalpr-lock-disk-headroom-v1",
        "measurement": "shutil.disk_usage.free/total",
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": ".",
        "total_bytes": total,
        "free_bytes": free,
        "free_fraction": round(fraction, 8),
        "minimum_fraction": DISK_HEADROOM_MIN_FRACTION,
        "passes": fraction >= DISK_HEADROOM_MIN_FRACTION,
    }


def validate_account_flat_proof(proof: Any, *, now: Any) -> list[str]:
    """Require a fresh, direct, read-only proof of a flat paper account."""
    if not isinstance(proof, dict):
        return ["account_flat_proof_missing"]
    problems = []
    if proof.get("schema_version") != ACCOUNT_FLAT_PROOF_VERSION:
        problems.append("account_flat_proof_schema_invalid")
    if proof.get("source") != "alpaca_trading_api_direct_uncached":
        problems.append("account_flat_proof_not_direct_uncached")
    if proof.get("mode") != "paper":
        problems.append("account_flat_proof_not_paper")
    if str(proof.get("account_status") or "").upper() != "ACTIVE":
        problems.append("account_flat_proof_account_not_active")
    identity_hash = str(proof.get("account_identity_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", identity_hash):
        problems.append("account_flat_proof_identity_invalid")
    if proof.get("flat") is not True:
        problems.append("account_not_flat")
    if proof.get("positions_count") != 0:
        problems.append("account_positions_nonzero")
    if proof.get("open_orders_count") != 0:
        problems.append("account_open_orders_nonzero")
    try:
        observed = _utc(proof.get("observed_at_utc"))
        age_seconds = (_utc(now) - observed).total_seconds()
        if age_seconds < -1.0:
            problems.append("account_flat_proof_from_future")
        elif age_seconds > ACCOUNT_FLAT_PROOF_MAX_AGE_SECONDS:
            problems.append("account_flat_proof_stale")
    except Exception:
        problems.append("account_flat_proof_timestamp_invalid")
    return problems


def validate_market_calendar_proof(proof: Any, *, target_session: Any,
                                   now: Any) -> list[str]:
    if not isinstance(proof, dict):
        return ["market_calendar_proof_missing"]
    problems = []
    if proof.get("schema_version") != MARKET_CALENDAR_PROOF_VERSION:
        problems.append("market_calendar_proof_schema_invalid")
    if proof.get("source") != "alpaca_trading_calendar_direct_uncached":
        problems.append("market_calendar_proof_not_direct_uncached")
    if proof.get("market") != "XNYS":
        problems.append("market_calendar_proof_market_invalid")
    if proof.get("target_session") != target_session:
        problems.append("market_calendar_proof_session_mismatch")
    if proof.get("is_trading_day") is not True:
        problems.append("target_session_not_trading_day")
    try:
        session_open = _utc(proof.get("market_open_utc"))
        session_close = _utc(proof.get("market_close_utc"))
        if session_close <= session_open:
            problems.append("market_calendar_bounds_invalid")
    except Exception:
        problems.append("market_calendar_bounds_invalid")
    try:
        observed = _utc(proof.get("observed_at_utc"))
        age_seconds = (_utc(now) - observed).total_seconds()
        if age_seconds < -1.0:
            problems.append("market_calendar_proof_from_future")
        elif age_seconds > MARKET_CALENDAR_PROOF_MAX_AGE_SECONDS:
            problems.append("market_calendar_proof_stale")
    except Exception:
        problems.append("market_calendar_proof_timestamp_invalid")
    return problems


def _resolve_evidence_path(value: Any, *, evidence_root: Path) -> Path:
    root = Path(evidence_root).resolve()
    candidate = Path(str(value or ""))
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    candidate.relative_to(root)
    return candidate


def validate_feed_sanity_evidence(reference: Any, *, target_session: Any,
                                  config_hash: str, evidence_root=ROOT) -> list[str]:
    """Verify the hash-bound, completed-RTH fast-path sanity report."""
    if not isinstance(reference, dict):
        return ["feed_sanity_evidence_missing"]
    problems = []
    try:
        path = _resolve_evidence_path(reference.get("evidence_path"),
                                      evidence_root=Path(evidence_root))
    except Exception:
        return ["feed_sanity_evidence_path_invalid"]
    stamped_hash = str(reference.get("evidence_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", stamped_hash):
        problems.append("feed_sanity_evidence_hash_invalid")
    try:
        actual_hash = _sha256_file(path)
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return sorted(set(problems + ["feed_sanity_evidence_unreadable"]))
    if stamped_hash != actual_hash:
        problems.append("feed_sanity_evidence_hash_mismatch")
    if report.get("schema_version") != FEED_SANITY_EVIDENCE_VERSION:
        problems.append("feed_sanity_evidence_schema_invalid")
    if report.get("mode") != "PAPER_SHADOW_ONLY":
        problems.append("feed_sanity_evidence_not_paper_shadow")
    if report.get("verdict") != "PASS" or report.get("operator_confirmed") is not True:
        problems.append("feed_sanity_evidence_not_approved_pass")
    if report.get("config_hash") != config_hash:
        problems.append("feed_sanity_evidence_config_mismatch")
    try:
        observed_session = date.fromisoformat(str(report.get("session_date")))
        lock_session = date.fromisoformat(str(target_session))
        if observed_session >= lock_session:
            problems.append("feed_sanity_evidence_not_prior_session")
    except ValueError:
        problems.append("feed_sanity_evidence_session_invalid")
    required_checks = {
        "trackable_plans", "eligible_contract_selection", "executable_bid_capture",
        "outcomes_0_ticks", "outcomes_1_tick", "outcomes_2_ticks",
        "direction_post_warmup", "gross_bar_quote_gaps", "session_continuity",
    }
    checks = report.get("checks") or {}
    for field in sorted(required_checks):
        if checks.get(field) != "PASS":
            problems.append(f"feed_sanity_{field}_not_pass")
    counts = report.get("counts") or {}
    minimum_positive = {
        "trackable_plans", "eligible_contracts", "executable_bid_records",
        "outcome_records_0_ticks", "outcome_records_1_tick",
        "outcome_records_2_ticks", "post_warmup_direction_evaluations",
    }
    for field in sorted(minimum_positive):
        try:
            if float(counts.get(field, 0)) <= 0:
                problems.append(f"feed_sanity_{field}_empty")
        except (TypeError, ValueError):
            problems.append(f"feed_sanity_{field}_invalid")
    try:
        if float(counts.get("post_warmup_direction_fresh_fraction")) <= 0.5:
            problems.append("feed_sanity_direction_not_mostly_fresh")
    except (TypeError, ValueError):
        problems.append("feed_sanity_direction_fresh_fraction_invalid")
    for field in ("late_guard_cycles", "connection_reset_errors", "gross_gap_count"):
        if counts.get(field) != 0:
            problems.append(f"feed_sanity_{field}_nonzero")
    return sorted(set(problems))


def validate_lock_candidate(document: dict, *, now: Any,
                            account_flat_proof: Any = None,
                            market_calendar_proof: Any = None,
                            feed_sanity_evidence: Any = None,
                            root=ROOT, evidence_root=ROOT,
                            disk_usage_fn=shutil.disk_usage) -> dict:
    """Fail closed unless a complete operator-confirmed candidate is pre-open."""
    problems = []
    if document.get("document_status") != "READY_TO_LOCK":
        problems.append("document_status_not_ready")
    if document.get("operator_confirmed") is not True:
        problems.append("operator_not_confirmed")
    if document.get("unconfirmed_fields"):
        problems.append("unconfirmed_fields_remain")
    proposed = document.get("proposed")
    if not isinstance(proposed, dict):
        problems.append("proposed_block_missing")
        proposed = {}
    placeholders = [value for value in _walk_strings(proposed)
                    if re.search(r"\b(?:PLACEHOLDER|CONFIRM)\b", value.upper())]
    if placeholders:
        problems.append("placeholder_values_remain")
    target_session = proposed.get("target_session")
    if not target_session:
        problems.append("target_session_missing")
    now_utc = _utc(now)
    problems.extend(validate_account_flat_proof(account_flat_proof, now=now_utc))
    problems.extend(validate_implementation_hashes(document, root=root))
    if target_session:
        try:
            session_date = date.fromisoformat(str(target_session))
            market_open = datetime.combine(session_date, time(9, 30), ET).astimezone(timezone.utc)
            if now_utc >= market_open:
                problems.append("lock_not_before_market_open")
        except ValueError:
            problems.append("target_session_invalid")
        problems.extend(validate_market_calendar_proof(
            market_calendar_proof, target_session=target_session, now=now_utc))
        config_hash = str((proposed.get("implementation_hashes") or {}).get("config_hash") or "")
        problems.extend(validate_feed_sanity_evidence(
            feed_sanity_evidence, target_session=target_session,
            config_hash=config_hash, evidence_root=evidence_root))
    try:
        disk = disk_headroom_proof(root=root, disk_usage_fn=disk_usage_fn)
        if not disk["passes"]:
            problems.append("disk_headroom_below_15_percent")
    except Exception:
        problems.append("disk_headroom_unverifiable")
    return {"valid": not problems, "problems": sorted(set(problems))}


def build_lock_record(document: dict, *, now: Any,
                      account_flat_proof: Any = None,
                      market_calendar_proof: Any = None,
                      feed_sanity_evidence: Any = None,
                      root=ROOT, evidence_root=ROOT,
                      disk_usage_fn=shutil.disk_usage) -> dict:
    validation = validate_lock_candidate(
        document, now=now, account_flat_proof=account_flat_proof,
        market_calendar_proof=market_calendar_proof,
        feed_sanity_evidence=feed_sanity_evidence, root=root,
        evidence_root=evidence_root, disk_usage_fn=disk_usage_fn)
    if not validation["valid"]:
        raise ValueError("cohort cannot be locked: " + ", ".join(validation["problems"]))
    disk_proof = disk_headroom_proof(root=root, disk_usage_fn=disk_usage_fn)
    if not disk_proof["passes"]:
        raise ValueError("cohort cannot be locked: disk_headroom_below_15_percent")
    locked_at = _utc(now).isoformat()
    frozen = {
        "cohort": document["proposed"],
        "locked_at_utc": locked_at,
        "operator_confirmation": document.get("operator_confirmation"),
        "account_flat_proof": account_flat_proof,
        "market_calendar_proof": market_calendar_proof,
        "feed_sanity_evidence": feed_sanity_evidence,
        "disk_headroom_proof": disk_proof,
    }
    frozen_hash = fe.canonical_hash(frozen)
    return {
        "schema_version": LOCK_RECORD_VERSION,
        "cohort_id": document["proposed"]["cohort_id"],
        "frozen_hash": frozen_hash,
        "locked_at_utc": locked_at,
        "frozen": frozen,
        "lock_record_id": fe.canonical_hash({
            "version": LOCK_RECORD_VERSION,
            "cohort_id": document["proposed"]["cohort_id"],
            "frozen_hash": frozen_hash,
        }),
        "paper_shadow_only": True,
    }


def append_lock(record: dict, path=DEFAULT_LOCK_REGISTRY) -> bool:
    existing = list(fe._iter_jsonl(path))
    same_id = [row for row in existing if row.get("cohort_id") == record.get("cohort_id")]
    if same_id:
        if any(row.get("frozen_hash") == record.get("frozen_hash") for row in same_id):
            return False
        raise ValueError("cohort_id already locked with a different hash; create a new cohort id")
    return fe._atomic_append(path, record)


def load_candidate(path) -> dict:
    with open(path) as handle:
        return json.load(handle)
