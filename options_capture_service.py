"""Orchestrates one explicit, read-only IVolatility evidence capture."""

from __future__ import annotations

from options_intelligence import (
    build_chain_snapshot, canonical_hash, engineer_options_features,
    normalize_ivolatility_row, utc_now,
)


class OptionsCaptureService:
    def __init__(self, client, store, *, clock=utc_now):
        self.client = client
        self.store = store
        self.clock = clock

    def capture_eod(self, *, symbol: str, trade_date: str) -> dict:
        """Fetch and persist one bounded chain; never submits or evaluates a trade."""
        raw_capture = self.client.fetch_eod_chain(symbol=symbol, trade_date=trade_date)
        raw_record = self.store.persist_raw(raw_capture)
        source_status = raw_capture.get("source_status", "unknown")
        if source_status != "ready":
            return {
                "source_status": source_status,
                "promoted": False,
                "raw_id": raw_record["raw_id"],
                "reason": "provider_response_not_complete",
            }

        contracts = []
        rejected = []
        for payload_index, payload in enumerate(raw_capture.get("payloads") or []):
            for row_index, row in enumerate(payload.get("data") or []):
                try:
                    contracts.append(normalize_ivolatility_row(row, underlying=symbol))
                except (TypeError, ValueError) as exc:
                    rejected.append({
                        "payload_index": payload_index,
                        "row_index": row_index,
                        "reason": str(exc)[:180],
                    })
        normalized_status = "degraded" if rejected else "ready"
        if not contracts:
            return {
                "source_status": "no_usable_contracts",
                "promoted": False,
                "contract_count": 0,
                "rejected_rows": rejected,
                "raw_id": raw_record["raw_id"],
                "reason": "no_rows_satisfied_the_versioned_contract",
            }
        snapshot = build_chain_snapshot(
            provider="ivolatility", underlying=symbol,
            captured_at=self.clock(), contracts=contracts,
            raw_payload_hash=canonical_hash(raw_capture),
            source_status=normalized_status,
        )
        features = engineer_options_features(snapshot)
        stored = self.store.persist(
            raw_capture=raw_capture, snapshot=snapshot, features=features)
        return {
            "source_status": normalized_status,
            "promoted": True,
            "contract_count": len(contracts),
            "rejected_rows": rejected,
            "raw_id": stored["raw_id"],
            "snapshot_id": stored["snapshot_id"],
            "feature_id": stored["feature_id"],
        }
