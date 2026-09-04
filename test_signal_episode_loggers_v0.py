import hashlib
import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import intraday_continuation_logger_v0 as study_b
import prior_regime_flip_reclaim_logger_v0 as study_a
import signal_episode_common_v0 as common


SESSION = "2026-09-08"
OPEN = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)


def quote(ts, mid):
    return {
        "ask": mid + .01, "bid": mid - .01, "mid": mid,
        "provider_ts": ts, "received_at": ts + timedelta(milliseconds=10),
        "receipt_age_seconds": .01, "source": "fixture:SIP",
    }


def flash(ts, *, session_date, regime="positive", spot=100, flip=100, call_wall=101):
    iso = ts.isoformat()
    return {
        "candidate_id": f"candidate-{iso}", "effective_ts": ts,
        "evidence": {
            "call_wall": {"price": call_wall}, "gamma_flip": flip,
            "gamma_regime": regime, "put_wall": {"price": 99}, "spot": spot,
            "data_freshness": {
                "as_of_by_endpoint": {"gex": iso, "levels": iso, "zero_dte": iso},
                "endpoint_states": {"gex": "AVAILABLE", "levels": "AVAILABLE", "zero_dte": "AVAILABLE"},
                "status": "FRESH",
            },
        },
        "observed_at_utc": iso, "record_hash": f"hash-{iso}",
        "record_type": "PIN_CANDIDATE", "session_date": session_date, "symbol": "SPY",
    }


def prereg(tmp_path, name):
    path = tmp_path / name
    path.write_text(f"frozen fixture: {name}\n")
    return path


def study_a_quotes(*, already_above=False):
    rows = []
    if already_above:
        rows.extend(quote(OPEN - timedelta(minutes=30) + timedelta(seconds=i), 99.8 if i < 60 else 100.1) for i in range(0, 1800, 5))
    for seconds in range(0, 76 * 60, 5):
        if already_above:
            mid = 100.1 + seconds * .00001
        else:
            mid = 99.8 if seconds < 10 else 100.1 + seconds * .00001
        rows.append(quote(OPEN + timedelta(seconds=seconds), mid))
    return rows


def negative_prior(*, stale=False):
    row = flash(
        datetime(2026, 9, 4, 19, 55, tzinfo=timezone.utc),
        session_date="2026-09-04", regime="negative", spot=99, flip=100,
    )
    if stale:
        row["evidence"]["data_freshness"]["status"] = "UNAVAILABLE_OR_STALE"
    return row


def study_b_quotes():
    rows = []
    for seconds in range(0, 121 * 60, 5):
        if seconds < 300:
            mid = 100.0
        elif seconds < 360:
            mid = 99.70
        else:
            mid = 100.0 + (seconds - 360) * .001
        rows.append(quote(OPEN + timedelta(seconds=seconds), mid))
    return rows


def test_frozen_documents_carry_exact_operator_values():
    prior = Path("PREREG_prior_regime_flip_reclaim_v0.md").read_text()
    continuation = Path("PREREG_intraday_continuation_v0.md").read_text()
    params = Path("PREREG_signal_params_RECOMMENDED_v0.md").read_text()
    assert "FROZEN / operator-approved" in prior and "2026-09-04" in prior
    assert "`A = 0.20` pts, `W = 900 s`, `G = 120 s`" in prior
    assert "N:** ≥ **150**" in prior and "p ≤ 0.01" in prior and "≥ 3 of 4" in prior
    assert "FROZEN / operator-approved" in continuation and "2026-09-04" in continuation
    assert "`S = 0.25` pts, `R = 180 s`, `V = 2 min`" in continuation
    assert "`≥ 3` consecutive positive 1-minute" in continuation
    assert "Executed volume is **EXCLUDED**" in continuation
    assert "Operator-Approved Frozen Parameters" in params
    report = Path("FREEZE_SIGNAL_PARAMS_EPISODE_LOGGERS_REPORT_v0.md").read_text()
    assert hashlib.sha256(Path("PREREG_prior_regime_flip_reclaim_v0.md").read_bytes()).hexdigest() in report
    assert hashlib.sha256(Path("PREREG_intraday_continuation_v0.md").read_bytes()).hexdigest() in report


def test_study_a_routes_rth_cross_and_freezes_prior_flip(tmp_path):
    frozen = prereg(tmp_path, "study-a.md")
    record = study_a.evaluate_session(
        session_date=SESSION, quotes=study_a_quotes(), flash_rows=[negative_prior()],
        prereg_path=frozen,
    )
    assert record["cohort"] == "H1b_RTH_CROSS_FROM_BELOW_OPEN"
    assert record["frozen_flip"] == 100
    assert record["counts_toward_n"] is True
    assert record["frozen_prereg_sha256"] == hashlib.sha256(frozen.read_bytes()).hexdigest()


def test_study_a_routes_observed_premarket_reclaim(tmp_path):
    record = study_a.evaluate_session(
        session_date=SESSION, quotes=study_a_quotes(already_above=True),
        flash_rows=[negative_prior()], prereg_path=prereg(tmp_path, "study-a.md"),
    )
    assert record["cohort"] == "H1a_PREMARKET_RECLAIM_RTH_ACCEPTANCE"
    assert record["counts_toward_n"] is True


def test_study_a_requires_observed_premarket_cross(tmp_path):
    rows = [
        quote(OPEN - timedelta(minutes=30) + timedelta(seconds=seconds), 100.1)
        for seconds in range(0, 30 * 60, 5)
    ]
    rows.extend(
        quote(OPEN + timedelta(seconds=seconds), 100.1)
        for seconds in range(0, 76 * 60, 5)
    )
    record = study_a.evaluate_session(
        session_date=SESSION, quotes=rows, flash_rows=[negative_prior()],
        prereg_path=prereg(tmp_path, "study-a.md"),
    )
    assert record["counts_toward_n"] is False
    assert record["exclusion_reason"] == "NO_OBSERVED_PREMARKET_RECLAIM"


def test_study_a_stale_regime_and_missing_horizon_are_excluded(tmp_path):
    frozen = prereg(tmp_path, "study-a.md")
    stale = study_a.evaluate_session(
        session_date=SESSION, quotes=study_a_quotes(), flash_rows=[negative_prior(stale=True)],
        prereg_path=frozen,
    )
    assert stale["exclusion_reason"] == "STALE_PRIOR_SESSION_REGIME"
    short = [row for row in study_a_quotes() if row["provider_ts"] < OPEN + timedelta(minutes=50)]
    missing = study_a.evaluate_session(
        session_date=SESSION, quotes=short, flash_rows=[negative_prior()], prereg_path=frozen,
    )
    assert missing["exclusion_reason"] == "A2_UNAVAILABLE_MISSING_POINT"
    assert missing["outcome"]["a2_outcome_status"] == "A2-UNAVAILABLE"


def test_no_fresh_anchor_is_unavailable_and_never_imputed():
    t0 = OPEN + timedelta(minutes=15)
    quotes = [quote(t0 - timedelta(seconds=6), 100), quote(t0 + timedelta(seconds=1), 101)]
    outcome = common.label_outcome(quotes, t0)
    assert outcome["a2_outcome_status"] == "A2-UNAVAILABLE"
    assert "anchor" in outcome["missing_points"]


def test_study_a_anchor_is_confirmation_provider_time(tmp_path):
    rows = study_a_quotes()
    crossing = next(index for index, row in enumerate(rows) if row["provider_ts"] >= OPEN and row["mid"] >= 100)
    target = rows[crossing]["provider_ts"] + timedelta(seconds=study_a.ACCEPTANCE_WINDOW_SECONDS)
    rows = [row for row in rows if row["provider_ts"] != target]
    rows.extend((quote(target - timedelta(seconds=4), 100.2), quote(target + timedelta(seconds=1), 100.2)))
    rows.sort(key=lambda row: row["provider_ts"])
    record = study_a.evaluate_session(
        session_date=SESSION, quotes=rows, flash_rows=[negative_prior()],
        prereg_path=prereg(tmp_path, "study-a.md"),
    )
    assert datetime.fromisoformat(record["anchor_t0_utc"]) == target + timedelta(seconds=1)
    assert record["provenance"]["acceptance"]["acceptance_completion"]["provider_ts"] == record["anchor_t0_utc"]


def test_study_b_markers_are_ordered_and_t0_is_migration_read(tmp_path):
    migration_ts = OPEN + timedelta(minutes=32)
    flashes = [
        flash(OPEN + timedelta(minutes=2), session_date=SESSION, call_wall=101.5),
        flash(migration_ts, session_date=SESSION, call_wall=102.0),
    ]
    record = study_b.evaluate_session(
        session_date=SESSION, quotes=study_b_quotes(), flash_rows=flashes,
        prereg_path=prereg(tmp_path, "study-b.md"),
    )
    assert record["counts_toward_n"] is True
    assert record["anchor_t0_utc"] == migration_ts.isoformat()
    assert record["provenance"]["proxy_vwap"]["basis"] == "quote_mid_proxy_vwap_not_traded_vwap"
    assert record["frozen_parameters"]["R_seconds"] == 180


def test_reclaim_without_prior_valid_sweep_never_triggers(tmp_path):
    monotonic = [quote(OPEN + timedelta(seconds=i), 100 + i * .0001) for i in range(0, 7200, 5)]
    record = study_b.evaluate_session(
        session_date=SESSION, quotes=monotonic,
        flash_rows=[flash(OPEN + timedelta(minutes=2), session_date=SESSION)],
        prereg_path=prereg(tmp_path, "study-b.md"),
    )
    assert record["counts_toward_n"] is False
    assert record["exclusion_reason"] == "NO_SWEEP_RECLAIM_SEQUENCE"


def test_study_b_crosses_latest_point_in_time_wall(tmp_path):
    rows = study_b_quotes()
    flashes = [
        flash(OPEN + timedelta(minutes=2), session_date=SESSION, call_wall=101.0),
        flash(OPEN + timedelta(minutes=20), session_date=SESSION, call_wall=101.5),
        flash(OPEN + timedelta(minutes=32), session_date=SESSION, call_wall=102.0),
    ]
    record = study_b.evaluate_session(
        session_date=SESSION, quotes=rows, flash_rows=flashes,
        prereg_path=prereg(tmp_path, "study-b.md"),
    )
    assert record["counts_toward_n"] is True
    assert record["provenance"]["wall_cross"]["call_wall"] == 101.5
    assert record["provenance"]["wall_migration"] == {"from": 101.5, "to": 102.0}


def test_prelock_rows_excluded_and_every_record_carries_prereg_hash(tmp_path):
    frozen = prereg(tmp_path, "frozen.md")
    a = study_a.evaluate_session(session_date="2026-09-04", quotes=[], flash_rows=[], prereg_path=frozen)
    b = study_b.evaluate_session(session_date="2026-09-04", quotes=[], flash_rows=[], prereg_path=frozen)
    expected = hashlib.sha256(frozen.read_bytes()).hexdigest()
    assert a["exclusion_reason"] == b["exclusion_reason"] == "PRELOCK_IN_SAMPLE"
    assert a["frozen_prereg_sha256"] == b["frozen_prereg_sha256"] == expected


def test_append_is_idempotent_and_logs_exclusions(tmp_path):
    frozen = prereg(tmp_path, "frozen.md")
    record = study_a.evaluate_session(session_date="2026-09-04", quotes=[], flash_rows=[], prereg_path=frozen)
    ledger = tmp_path / "study.jsonl"
    assert common.append_once(ledger, record) is True
    assert common.append_once(ledger, record) is False
    assert len(ledger.read_text().splitlines()) == 1


def test_loggers_are_isolated_from_prohibited_paths_and_trigger_excludes_trade_count():
    sources = "\n".join(inspect.getsource(module) for module in (common, study_a, study_b)).lower()
    for forbidden in ("scalp_server", "broker", "order_adapter", "guard_events", "admission_authority"):
        assert forbidden not in sources
    assert "import a2_measurement" not in sources
    trigger_source = inspect.getsource(study_b.sweep_reclaims) + inspect.getsource(study_b.evaluate_session)
    assert "volume" not in trigger_source.lower()
