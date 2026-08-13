#!/usr/bin/env python3
"""Offline, causal audit of legacy UNTRACKABLE Entry Intelligence episodes.

The Aug-07 v1 manifests retained rejection reasons but not the full OPRA quote
records.  This audit never reconstructs or retimestamps those quotes.  It
verifies the manifests, reassembles only the fields that are actually
recoverable at decision time (OCC metadata, same-session chain volume/OI/IV,
and local-BS delta from the frozen technical close), and reports the quote
evidence gap explicitly.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import decision_replay_v1 as replay
import entry_contract_data_v1 as contract_data
import feature_engine as fe
import scalpr_config


ROOT = Path(__file__).resolve().parent
REPLAY_VERSION = "contract-data-fix-offline-replay-v1"


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(
        tzinfo=timezone.utc)


def _by(rows: list[dict], key: str) -> dict[str, dict]:
    return {str(row.get(key)): row for row in rows if row.get(key)}


def audit(*, root: Path = ROOT, session_date: str) -> dict:
    config = scalpr_config.load_entry_intelligence_config(
        root / "config" / "scalpr.yaml")
    workup_path = root / "runs" / f"SPY_{session_date}.json"
    workup = json.loads(workup_path.read_text(encoding="utf-8"))
    chain_rows = list(workup.get("contracts") or [])
    plans = [
        row for row in fe._iter_jsonl(
            root / config["collector"]["no_trade_plan_log"])
        if row.get("status") == "UNTRACKABLE"
        and str(row.get("created_at", ""))[:10] == session_date
    ]
    decisions = _by(list(fe._iter_jsonl(
        root / config["collector"]["decision_log"])), "decision_id")
    manifests = _by(list(fe._iter_jsonl(
        root / config["collector"]["evidence_manifest_log"])), "decision_id")
    sources = list(fe._iter_jsonl(root / config["collector"]["evidence_source_log"]))
    sources_by_decision: dict[str, list[dict]] = {}
    for source in sources:
        sources_by_decision.setdefault(str(source.get("decision_id")), []).append(source)

    audited = []
    for plan in plans:
        decision_id = str(plan["decision_id"])
        decision = decisions.get(decision_id)
        manifest = manifests.get(decision_id)
        verification = (
            replay.verify_evidence_manifest(
                manifest, root / config["collector"]["evidence_source_log"],
                decision_packet=decision)
            if manifest and decision else {
                "status": "UNRECOVERABLE",
                "failures": [{"reason": "DECISION_OR_MANIFEST_MISSING"}],
                "current_data_fallback_used": False,
            })
        decision_sources = sources_by_decision.get(decision_id, [])
        technical = next((row for row in decision_sources
                          if row.get("source_type") == "TECHNICAL_SETUP"), None)
        legacy_selection = next((row for row in decision_sources
                                 if row.get("source_type") == "CONTRACT_SELECTION"), None)
        decided_at = _utc((decision or plan).get("decided_at") or plan["created_at"])
        setup = (technical or {}).get("content") or {}
        spot = (setup.get("evidence") or {}).get("decision_bar_close")
        side = str(plan["side"])
        right = "C" if side == "CALL" else "P"
        recovered = []
        side_contract_count = 0
        delta_computable_count = 0
        chain_volume_observed_count = 0
        chain_oi_observed_count = 0
        for chain in chain_rows:
            if str(chain.get("type") or "").upper() != right:
                continue
            side_contract_count += 1
            assembled = contract_data.assemble_contract(
                chain=chain, chain_observed_at=workup.get("as_of"),
                snapshot={
                    "bid": None, "ask": None, "quote_observed_at": None,
                    "received_at": decided_at, "delta": None,
                    "snapshot_volume": None,
                },
                underlying={
                    "spot": spot, "observed_at": decided_at,
                    "received_at": decided_at,
                    "source": "frozen_technical_setup_decision_bar_close",
                },
                decided_at=decided_at,
                config=config["contract_assembly"],
            )
            if assembled.get("delta") is not None:
                delta_computable_count += 1
            if assembled.get("volume") is not None:
                chain_volume_observed_count += 1
            if assembled.get("open_interest") is not None:
                chain_oi_observed_count += 1
            if (assembled.get("delta") is not None
                    and float(config["execution"]["delta_min"])
                    <= abs(float(assembled["delta"]))
                    <= float(config["execution"]["delta_max"])
                    and assembled.get("volume") is not None
                    and int(assembled["volume"])
                    >= int(config["execution"]["min_contract_volume"])
                    and assembled.get("open_interest") is not None
                    and int(assembled["open_interest"])
                    >= int(config["execution"]["min_open_interest"])):
                recovered.append({
                    "option_symbol": assembled["option_symbol"],
                    "delta": assembled["delta"],
                    "delta_source": assembled["delta_source"],
                    "volume": assembled["volume"],
                    "open_interest": assembled["open_interest"],
                    "contract_assembly_hash": assembled["contract_assembly_hash"],
                })
        legacy_reasons = sorted({
            reason
            for rejection in ((legacy_selection or {}).get("content") or {}).get(
                "rejected", [])
            for reason in rejection.get("reasons", [])
        })
        audited.append({
            "decision_id": decision_id,
            "side": side,
            "decided_at": decided_at.isoformat(),
            "manifest_verification": verification,
            "legacy_rejection_reasons": legacy_reasons,
            "recoverable_delta_volume_oi_candidates": recovered,
            "recoverable_candidate_count": len(recovered),
            "side_contract_count": side_contract_count,
            "delta_computable_count": delta_computable_count,
            "chain_volume_observed_count": chain_volume_observed_count,
            "chain_open_interest_observed_count": chain_oi_observed_count,
            "assembly_field_repair_status": (
                "PASS" if side_contract_count > 0
                and delta_computable_count == side_contract_count
                and chain_volume_observed_count == side_contract_count
                and chain_oi_observed_count == side_contract_count else "FAIL"),
            "approved_static_gate_result": (
                "PASS_CANDIDATE_EXISTS" if recovered
                else "FAIL_NO_CONTRACT_IN_APPROVED_DELTA_VOLUME_OI_BAND"),
            "historical_opra_quote_state": "UNAVAILABLE",
            "replay_status": "UNRECOVERABLE_HISTORICAL_OPRA_QUOTE_NOT_RETAINED",
            "tracking_plan_fabricated": False,
            "current_or_future_market_data_used": False,
        })

    all_verified = bool(audited) and all(
        row["manifest_verification"]["status"] == "VERIFIED" for row in audited)
    all_static_repaired = bool(audited) and all(
        row["assembly_field_repair_status"] == "PASS" for row in audited)
    result = {
        "schema_version": "contract-data-fix-offline-replay-report-v1",
        "replay_version": REPLAY_VERSION,
        "session_date": session_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_version": config["config_version"],
        "config_hash": config["config_hash"],
        "contract_assembly_version": contract_data.CONTRACT_ASSEMBLY_VERSION,
        "contract_assembly_hash": contract_data.contract_assembly_hash(
            config["contract_assembly"]),
        "episode_count": len(audited),
        "manifest_integrity": "PASS" if all_verified else "FAIL",
        "delta_volume_oi_repair": "PASS" if all_static_repaired else "FAIL",
        "point_in_time_quote_replay": "UNRECOVERABLE_LEGACY_EVIDENCE_GAP",
        "acceptance_status": (
            "PASS_WITH_EXPLICIT_LEGACY_QUOTE_LIMITATION"
            if all_verified and all_static_repaired else "FAIL"),
        "note": (
            "A historical TRACKABLE plan was not created because decision-time "
            "OPRA quote values/timestamps were not retained in the v1 rejection "
            "manifest. Reconstructing them later would be look-ahead. Future v2 "
            "selection manifests retain every assembled rejected record."),
        "episodes": audited,
    }
    result["report_hash"] = fe.canonical_hash({
        key: value for key, value in result.items()
        if key not in {"generated_at", "report_hash"}
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = audit(session_date=args.session_date)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    print(json.dumps({
        key: report[key] for key in (
            "session_date", "episode_count", "manifest_integrity",
            "delta_volume_oi_repair", "point_in_time_quote_replay",
            "acceptance_status", "report_hash")
    }, indent=2, sort_keys=True))
    return 0 if report["acceptance_status"].startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
