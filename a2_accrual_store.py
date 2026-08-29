"""Canonical read interface for the authoritative dense A2 accrual store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DENSE_ENDPOINT_SOURCE = "alpaca_historical_stock_quote_v1"
DENSE_OUTPUT_DIR = Path("v2_data") / "a2_measurement"
DENSE_LABELS_PATH = DENSE_OUTPUT_DIR / "a2_labels_dense_v0.jsonl"
DENSE_SUMMARY_PATH = DENSE_OUTPUT_DIR / "a2_summary_dense_v0.json"
DENSE_COMPARISON_PATH = DENSE_OUTPUT_DIR / "a2_dense_source_comparison_v0.json"
DENSE_STATUS_PATH = DENSE_OUTPUT_DIR / "a2_accrual_status_v0.json"


class DenseAccrualStoreError(RuntimeError):
    """The authoritative dense accrual summary is absent or invalid."""


def load_dense_summary(path: Path = DENSE_SUMMARY_PATH) -> dict[str, Any]:
    """Load and validate the only summary authorized for A2 accrual counts."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DenseAccrualStoreError(f"dense_a2_summary_missing={path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DenseAccrualStoreError(
            f"dense_a2_summary_unreadable={path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise DenseAccrualStoreError(f"dense_a2_summary_not_object={path}")
    if payload.get("endpoint_source") != DENSE_ENDPOINT_SOURCE:
        raise DenseAccrualStoreError(
            "dense_a2_summary_provenance_mismatch="
            f"{payload.get('endpoint_source')!r}"
        )
    count = payload.get("clean_a2_labelable_episode_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise DenseAccrualStoreError(
            f"dense_a2_summary_invalid_clean_labelable_count={count!r}"
        )
    return {
        **payload,
        "accrual_store": "dense_a2_v0",
        "accrual_summary_path": str(path),
    }


def load_dense_labels(path: Path = DENSE_LABELS_PATH) -> list[dict[str, Any]]:
    """Load the dense label ledger and reject mixed or malformed provenance."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise DenseAccrualStoreError(f"dense_a2_labels_missing={path}") from exc
    except OSError as exc:
        raise DenseAccrualStoreError(
            f"dense_a2_labels_unreadable={path}: {type(exc).__name__}: {exc}"
        ) from exc
    labels: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DenseAccrualStoreError(
                f"dense_a2_label_invalid_json_line={line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise DenseAccrualStoreError(
                f"dense_a2_label_not_object_line={line_number}")
        if row.get("endpoint_source") != DENSE_ENDPOINT_SOURCE:
            raise DenseAccrualStoreError(
                "dense_a2_label_provenance_mismatch_line="
                f"{line_number}:{row.get('endpoint_source')!r}"
            )
        labels.append(row)
    return labels
