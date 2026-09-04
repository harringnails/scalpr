#!/usr/bin/env python3
"""Append-only Study A evaluator for frozen prior-regime flip reclaims."""

from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path
from typing import Any

import signal_episode_common_v0 as common


STUDY_ID = "prior_regime_flip_reclaim_v0"
ACCEPTANCE_BAND_POINTS = 0.20
ACCEPTANCE_WINDOW_SECONDS = 900
BACK_BELOW_GRACE_SECONDS = 120
DEFAULT_OUTPUT = Path("prior_regime_flip_reclaim_v0.jsonl")
DEFAULT_PREREG = Path("PREREG_prior_regime_flip_reclaim_v0.md")


def excluded(record: dict[str, Any], reason: str, **fields: Any) -> dict[str, Any]:
    return common.finalize_record({
        **record, **fields, "counts_toward_n": False, "exclusion_reason": reason,
        "episode_status": "EXCLUDED",
    })


def acceptance_anchor(
    quotes: list[dict[str, Any]], *, start_index: int, flip: float,
) -> tuple[Any, str | None, dict[str, Any]]:
    start = quotes[start_index]
    target = start["provider_ts"] + timedelta(seconds=ACCEPTANCE_WINDOW_SECONDS)
    end = common.earliest_at_or_after(quotes, target)
    provenance = {
        "acceptance_start": common.quote_provenance(start, start["provider_ts"]),
        "acceptance_target_utc": target.isoformat(),
    }
    if end is None:
        return None, "MISSING_ACCEPTANCE_ENDPOINT", provenance
    path = [
        quote for quote in quotes[start_index:]
        if quote["provider_ts"] <= end["provider_ts"]
    ]
    if common.gap_exceeds(path):
        return None, "ACCEPTANCE_QUOTE_GAP_OVER_5S", provenance
    floor = flip - ACCEPTANCE_BAND_POINTS
    if any(quote["mid"] < floor for quote in path):
        return None, "ACCEPTANCE_BAND_BREACH", provenance
    cumulative_back_below = 0.0
    for left, right in zip(path, path[1:]):
        if left["mid"] < flip:
            cumulative_back_below += (right["provider_ts"] - left["provider_ts"]).total_seconds()
        if cumulative_back_below > BACK_BELOW_GRACE_SECONDS:
            return None, "BACK_BELOW_GRACE_EXCEEDED", provenance
    provenance.update({
        "acceptance_completion": common.quote_provenance(end, target),
        "cumulative_back_below_seconds": round(cumulative_back_below, 6),
        "minimum_mid": round(min(quote["mid"] for quote in path), 6),
    })
    # The hold is observable only when the first quote at/after the target arrives.
    return end["provider_ts"], None, provenance


def first_cross_before(
    quotes: list[dict[str, Any]], *, level: float, before: Any,
) -> tuple[int, dict[str, Any]] | None:
    previous = None
    for index, quote in enumerate(quotes):
        if quote["provider_ts"] >= before:
            break
        if previous is not None and previous["mid"] < level <= quote["mid"]:
            return index, quote
        previous = quote
    return None


def evaluate_session(
    *, session_date: str, quotes: list[dict[str, Any]],
    flash_rows: list[dict[str, Any]], prereg_path: Path,
) -> dict[str, Any]:
    record = common.base_record(study_id=STUDY_ID, session_date=session_date, prereg_path=prereg_path)
    record["frozen_parameters"] = {
        "A_points": ACCEPTANCE_BAND_POINTS,
        "G_seconds": BACK_BELOW_GRACE_SECONDS,
        "N_per_cohort": 150,
        "W_seconds": ACCEPTANCE_WINDOW_SECONDS,
        "p_max": 0.01,
        "walk_forward": "4_fold_chronological_sign_consistent_at_least_3_of_4",
    }
    if common.prelock(session_date):
        return excluded(record, "PRELOCK_IN_SAMPLE")

    prior_dates = sorted({
        row.get("session_date") for row in flash_rows
        if row.get("session_date") and row.get("session_date") < session_date
    })
    if not prior_dates:
        return excluded(record, "MISSING_PRIOR_SESSION_REGIME")
    prior_date = prior_dates[-1]
    prior_candidates = sorted(
        (row for row in flash_rows if row.get("session_date") == prior_date),
        key=lambda row: row["effective_ts"],
    )
    fresh_prior = [row for row in prior_candidates if common.flash_is_fresh(row)]
    if not fresh_prior:
        stale_provenance = common.flash_provenance(prior_candidates[-1]) if prior_candidates else {"status": "MISSING"}
        return excluded(
            record, "STALE_PRIOR_SESSION_REGIME", prior_session_date=prior_date,
            provenance={"prior_close_regime": stale_provenance},
        )
    prior = fresh_prior[-1]
    values = common.flash_values(prior)
    flip, spot = values["gamma_flip"], values["spot"]
    provenance = {"prior_close_regime": common.flash_provenance(prior)}
    if flip is None or spot is None or values["gamma_regime"] is None:
        return excluded(record, "MISSING_PRIOR_REGIME_FIELDS", provenance=provenance)
    if values["gamma_regime"] != "negative" or spot >= flip:
        return excluded(record, "PRIOR_NEGATIVE_GAMMA_GATE_NOT_MET", provenance=provenance)
    if not quotes:
        return excluded(record, "MISSING_SESSION_QUOTES", provenance=provenance)

    open_at, close_at = common.session_bounds(session_date)
    open_quote = common.earliest_at_or_after(quotes, open_at)
    if open_quote is None:
        return excluded(record, "NO_FRESH_OPEN_QUOTE", provenance=provenance)
    open_index = quotes.index(open_quote)
    premarket_reclaim = first_cross_before(quotes, level=flip, before=open_at)
    if open_quote["mid"] >= flip:
        if premarket_reclaim is None:
            return excluded(record, "NO_OBSERVED_PREMARKET_RECLAIM", provenance=provenance)
        cohort = "H1a_PREMARKET_RECLAIM_RTH_ACCEPTANCE"
        reclaim_index, reclaim_quote = premarket_reclaim
        acceptance_start_index = open_index
    else:
        rth_cross = next((
            (index, quote) for index, quote in enumerate(quotes[open_index:], open_index)
            if quote["provider_ts"] < close_at and quote["mid"] >= flip
        ), None)
        if rth_cross is None:
            return excluded(record, "NO_RTH_RECLAIM", provenance=provenance)
        cohort = "H1b_RTH_CROSS_FROM_BELOW_OPEN"
        reclaim_index, reclaim_quote = rth_cross
        acceptance_start_index = reclaim_index

    provenance.update({
        "frozen_flip": {
            "F": flip, "frozen_at_open_utc": open_at.isoformat(),
            "source_prior_session": prior_date,
        },
        "open_quote": common.quote_provenance(open_quote, open_at),
        "reclaim_quote": common.quote_provenance(reclaim_quote, reclaim_quote["provider_ts"]),
    })
    t0, failure, acceptance_provenance = acceptance_anchor(
        quotes, start_index=acceptance_start_index, flip=flip,
    )
    provenance["acceptance"] = acceptance_provenance
    if failure:
        return excluded(record, failure, cohort=cohort, frozen_flip=flip, provenance=provenance)

    outcome = common.label_outcome(quotes, t0)
    provenance["outcome"] = outcome["provenance"]
    if outcome["a2_outcome_status"] != "A2-AVAILABLE":
        return excluded(
            record, "A2_UNAVAILABLE_MISSING_POINT", cohort=cohort, frozen_flip=flip,
            anchor_t0_utc=t0.isoformat(), outcome=outcome, provenance=provenance,
        )
    return common.finalize_record({
        **record,
        "anchor_t0_utc": t0.isoformat(),
        "cohort": cohort,
        "counts_toward_n": True,
        "episode_status": "A2-AVAILABLE",
        "exclusion_reason": None,
        "frozen_flip": flip,
        "outcome": outcome,
        "provenance": provenance,
    })


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("evaluate",))
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--tick-log", type=Path, default=Path("tick_log.csv"))
    parser.add_argument("--flashalpha-ledger", type=Path, required=True)
    parser.add_argument("--prereg", type=Path, default=DEFAULT_PREREG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    record = evaluate_session(
        session_date=args.session_date,
        quotes=common.load_session_quotes(args.tick_log, args.session_date, include_premarket=True),
        flash_rows=common.load_flash_candidates(args.flashalpha_ledger),
        prereg_path=args.prereg,
    )
    written = common.append_once(args.output, record)
    print({
        "counts_toward_n": record["counts_toward_n"],
        "episode_status": record["episode_status"],
        "record_hash": record["record_hash"],
        "written": written,
    })


if __name__ == "__main__":
    main()
