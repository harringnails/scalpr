"""Non-overlapping Entry Intelligence episodes and a session-aware null test."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import feature_engine as fe


EPISODE_RECORD_VERSION = "entry-reversal-episode-v1"
NULL_VERSION = "entry-episode-session-block-sign-null-v1"
DEFAULT_EPISODE_LOG = Path("entry_intelligence_episodes_v1.jsonl")


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def admit_episode(candidate: dict, *, existing: list[dict], outcome_horizon_minutes: int,
                  cooldown_minutes: int) -> dict:
    """First qualifying reference only; reject overlap and cooldown reuse."""
    decided = _utc(candidate["decided_at"])
    key = candidate["episode_key"]
    same_scope = [row for row in existing if row.get("symbol") == candidate.get("symbol")
                  and row.get("side") == candidate.get("side")]
    reason = None
    if any(row.get("episode_key") == key and row.get("admitted") for row in same_scope):
        reason = "DUPLICATE_REVERSAL_REFERENCE"
    else:
        block_for = timedelta(minutes=max(int(outcome_horizon_minutes), int(cooldown_minutes)))
        prior = [row for row in same_scope if row.get("admitted") and _utc(row["decided_at"]) <= decided]
        if prior and decided < max(_utc(row["decided_at"]) for row in prior) + block_for:
            reason = "OVERLAPPING_OUTCOME_OR_COOLDOWN"
    record = {
        "schema_version": EPISODE_RECORD_VERSION,
        "config_version": candidate["config_version"],
        "config_hash": candidate["config_hash"],
        "episode_record_id": fe.canonical_hash({
            "version": EPISODE_RECORD_VERSION, "episode_key": key,
            "decision_id": candidate.get("decision_id"),
            "episode_kind": candidate.get("episode_kind", "TRADE"),
            "decided_at": decided.isoformat(), "reason": reason,
        }),
        "episode_key": key, "decision_id": candidate.get("decision_id"),
        "cohort_id": candidate.get("cohort_id"), "symbol": candidate.get("symbol"),
        "side": candidate.get("side"), "session_date": candidate.get("session_date"),
        "episode_kind": candidate.get("episode_kind", "TRADE"),
        "decided_at": decided.isoformat(), "admitted": reason is None,
        "rejection_reason": reason, "execution_authority": False,
    }
    return record


def append_episode(record: dict, path=DEFAULT_EPISODE_LOG) -> bool:
    if any(row.get("episode_record_id") == record.get("episode_record_id") for row in fe._iter_jsonl(path)):
        return False
    return fe._atomic_append(path, record)


def session_block_sign_null(episodes: list[dict], *, n_permutations: int = 5000,
                            block_sessions: int = 5, seed: int = 20260805) -> dict:
    """Test mean incremental return against zero without breaking session groups.

    Each record supplies `session_date` and `incremental_return`, where abstain is
    explicitly zero or another pre-registered baseline.  Contiguous session
    blocks receive one random sign, retaining within-session and short-regime
    dependence.  The controlling p-value uses the finite-sample +1 correction.
    """
    clean = [row for row in episodes if row.get("incremental_return") is not None]
    clean.sort(key=lambda row: (row["session_date"], row.get("decided_at", "")))
    if not clean:
        return {"available": False, "reason": "no_labeled_episodes", "null_version": NULL_VERSION}
    sessions = []
    for row in clean:
        if row["session_date"] not in sessions:
            sessions.append(row["session_date"])
    block_sessions = max(1, int(block_sessions))
    blocks = [set(sessions[i:i + block_sessions]) for i in range(0, len(sessions), block_sessions)]
    values = [float(row["incremental_return"]) for row in clean]
    observed = mean(values)
    rng = random.Random(seed)
    null = []
    for _ in range(int(n_permutations)):
        signs = {}
        # Keep one sign per contiguous block, not one sign per session.
        for block in blocks:
            sign = 1 if rng.random() >= 0.5 else -1
            for session in block:
                signs[session] = sign
        null.append(mean(float(row["incremental_return"]) * signs[row["session_date"]]
                         for row in clean))
    p_value = (1 + sum(value >= observed for value in null)) / (len(null) + 1)
    return {
        "available": True, "null_version": NULL_VERSION,
        "metric": "mean_incremental_executable_bid_return",
        "higher_is_better": True, "n_episodes": len(clean),
        "n_sessions": len(sessions), "block_sessions": block_sessions,
        "n_permutations": int(n_permutations), "seed": seed,
        "observed_mean": round(observed, 10),
        "one_sided_p_value": round(p_value, 10),
        "finite_sample_plus_one": True,
        "interpretation": "research-only; requires frozen holdout and realistic costs",
    }
