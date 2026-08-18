"""Stage-1a feature-distribution viability report for clean regime tags."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import entry_episode_integrity_v1 as episode_integrity
import feature_engine as fe
import regime_layer_v0


REPORT_VERSION = "regime-distribution-viability-v0"
CRITERIA_VERSION = "regime-distribution-viability-criteria-v0"
ELIGIBLE_CONFIG_VERSION = "entry-intelligence-config-v1.2.0"
CLASSIFIED_STATES = (
    "HIGH_VOL", "TREND_UP", "TREND_DOWN", "RANGE", "TRANSITIONAL",
)
UNKNOWN_STATE = "UNKNOWN"
MAX_SINGLE_STATE_FRACTION = 0.70
MIN_STATES_AT_CELL_SIZE = 2
MIN_CELL_SIZE = 30
MAX_UNKNOWN_FRACTION = 0.20
DEFAULT_EPISODE_LOG = Path("entry_intelligence_episodes_v1.jsonl")
DEFAULT_QUARANTINE_LOG = episode_integrity.DEFAULT_QUARANTINE_MANIFEST
DEFAULT_REPORT_PATH = Path("v2_data") / "regime_stage1a_viability_v0.json"


def _fraction(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def load_clean_v1_2_episodes(
    episodes_path: Path | str = DEFAULT_EPISODE_LOG,
    quarantine_path: Path | str | None = None,
) -> list[dict]:
    """Load admitted v1.2 rows not named by the immutable quarantine manifest."""
    episodes_path = Path(episodes_path)
    quarantine_path = Path(quarantine_path or episodes_path.with_name(DEFAULT_QUARANTINE_LOG.name))
    quarantined = episode_integrity.quarantined_episode_record_ids(quarantine_path)
    return [
        row for row in fe._iter_jsonl(episodes_path)
        if row.get("admitted") is True
        and row.get("config_version") == ELIGIBLE_CONFIG_VERSION
        and str(row.get("episode_record_id") or "") not in quarantined
    ]


def _state(episode: dict) -> str:
    state = str((episode.get("regime_tag") or {}).get("state") or UNKNOWN_STATE).upper()
    return state if state in regime_layer_v0.REGIME_STATES else UNKNOWN_STATE


def build_viability_report(
    episodes: Iterable[dict], *, generated_at: datetime | None = None
) -> dict:
    """Characterize regime tags only; no outcome values are read or computed."""
    rows = list(episodes)
    counts = Counter(_state(row) for row in rows)
    total = len(rows)
    unknown_count = counts[UNKNOWN_STATE]
    classified_count = total - unknown_count
    raw_unknown_fraction = unknown_count / total if total else None
    unknown_fraction = _fraction(unknown_count, total)

    states = {
        state: {
            "count": counts[state],
            "fraction_of_all_clean_episodes": _fraction(counts[state], total),
            "fraction_of_classified_episodes": _fraction(counts[state], classified_count),
        }
        for state in CLASSIFIED_STATES
    }
    largest_fraction = max(
        (counts[state] / classified_count for state in CLASSIFIED_STATES),
        default=0.0,
    ) if classified_count else 0.0
    states_at_minimum = sum(item["count"] >= MIN_CELL_SIZE for item in states.values())
    unknown_pass = (
        raw_unknown_fraction is not None
        and raw_unknown_fraction < MAX_UNKNOWN_FRACTION
    )
    concentration_pass = (
        classified_count > 0 and largest_fraction <= MAX_SINGLE_STATE_FRACTION
    )
    cell_size_pass = states_at_minimum >= MIN_STATES_AT_CELL_SIZE

    if total == 0 or not unknown_pass:
        verdict = "INSUFFICIENT_TAGGING"
    elif not concentration_pass or not cell_size_pass:
        verdict = "LOW_CONTRAST"
    else:
        verdict = "VIABLE"

    return {
        "schema_version": REPORT_VERSION,
        "criteria_version": CRITERIA_VERSION,
        "generated_at": (generated_at or datetime.now(timezone.utc)).astimezone(
            timezone.utc).isoformat(),
        "scope": "CLEAN_NON_QUARANTINED_V1_2_EPISODES",
        "eligible_config_version": ELIGIBLE_CONFIG_VERSION,
        "n_clean_episodes": total,
        "n_classified_episodes": classified_count,
        "classified_states": states,
        "unknown": {
            "count": unknown_count,
            "fraction_of_all_clean_episodes": unknown_fraction,
        },
        "criteria": {
            "single_state_max_fraction_of_classified": MAX_SINGLE_STATE_FRACTION,
            "single_state_must_not_exceed_max": True,
            "largest_observed_state_fraction_of_classified": (
                round(largest_fraction, 6) if classified_count else None),
            "concentration_pass": concentration_pass,
            "minimum_states_at_cell_size": MIN_STATES_AT_CELL_SIZE,
            "minimum_cell_size": MIN_CELL_SIZE,
            "observed_states_at_minimum_cell_size": states_at_minimum,
            "cell_size_pass": cell_size_pass,
            "unknown_fraction_must_be_below": MAX_UNKNOWN_FRACTION,
            "unknown_fraction_pass": unknown_pass,
        },
        "verdict": verdict,
        "verdict_scope": "FEATURE_DISTRIBUTION_ONLY_NOT_AN_EDGE_RESULT",
        "outcome_fields_read": [],
        "outcome_computation_performed": False,
        "edge_computation_performed": False,
        "stage_2_filter_deployed": False,
        "execution_authority": False,
    }


def write_report(report: dict, path: Path | str = DEFAULT_REPORT_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    report = build_viability_report(load_clean_v1_2_episodes())
    write_report(report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
