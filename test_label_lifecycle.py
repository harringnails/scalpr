"""
Tests for label_lifecycle.py (label-lifecycle-v0) + the decision-time snapshot.

Verifies the lifecycle discipline, not profitability:
  * decision-time snapshot is immutable + idempotent (never regenerated);
  * labels use only forward bars (no leak) and preserve the proxy flags;
  * horizon respected: PENDING until filled / hit / expiry; multi-session ok;
  * FINAL / UNLABELABLE / ERROR_RETRYABLE states;
  * idempotent rerun = no-op; version bump with differing result = versioned
    correction carrying prior+new hashes and a reason (both records preserved);
  * per-contract failure isolation (one bad contract doesn't abort the run);
  * the full required field set is present.

Run: python3 test_label_lifecycle.py
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import feature_engine as fe
import label_lifecycle as ll

TMP = tempfile.mkdtemp()
SNAPS = os.path.join(TMP, "snaps.jsonl")
LABELS = os.path.join(TMP, "labels.jsonl")

DTS = "2026-07-27T14:00:00+00:00"        # decision timestamp (Mon 10:00 ET)
FAR_EXPIRY = "2026-08-21"


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok: {name}")


def _bar(minute, hi, lo, cl):
    ts = f"2026-07-27T14:{minute:02d}:00+00:00"
    return {"ts": ts, "high": hi, "low": lo, "close": cl}


def _contract(sym="C1", mid=1.0, delta=0.5, gamma=0.0, typ="C", expiry=FAR_EXPIRY):
    fq = {k: None for k in fe.FROZEN_QUOTE_FIELDS}
    fq.update({"symbol": sym, "mid": mid, "delta": delta, "gamma": gamma,
               "type": typ, "expiry": expiry})
    return {**fq, "contract_snapshot_hash": fe.canonical_hash(fq)}


def _write_snapshot(contracts, horizon=3, u0=100.0, path=SNAPS, decision_ts=DTS):
    fr_stub = {"ticker": "SPY", "timestamp": decision_ts}
    snap = {"decision_id": f"SPY:2026-07-27:{decision_ts[11:13]}{decision_ts[14:16]}",
            "schema_version": fe.SCHEMA_VERSION, "formal_cohort_eligible": False,
            "ticker": "SPY", "market_date": "2026-07-27",
            "decision_timestamp": decision_ts, "session_minute": 30,
            "underlying_at_decision": u0, "label_horizon_min": horizon,
            "label_target_pct": fe.LABEL_TARGET_PCT, "label_stop_pct": fe.LABEL_STOP_PCT,
            "feature_snapshot_hash": fe.canonical_hash(fr_stub),
            "feature_record": fr_stub, "contract_universe": contracts}
    with open(path, "w") as f:
        f.write(json.dumps(snap) + "\n")


def _reset_labels():
    if os.path.exists(LABELS):
        os.remove(LABELS)


NOW = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)     # after bars, expiry future
NOW_AFTER_EXPIRY = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)


# ── decision-time snapshot immutability + idempotency ──────────────────────
def test_snapshot_immutable_idempotent():
    print("Decision-time snapshot writes once and is never regenerated")
    path = os.path.join(TMP, "persist.jsonl")
    fe.SNAPSHOT_DEDUP_LOG = os.path.join(TMP, "dedup.jsonl")   # don't pollute cwd
    payload = {"spot": 100.0, "iv_rank": 30,
               "contracts": [{"symbol": "Z", "type": "C", "delta": 0.5, "mid": 1.0,
                              "gamma": 0.0, "expiry": FAR_EXPIRY}]}
    with patch("regime_model.regime_read", return_value={"available": False}):
        fr = fe.build_feature_record(None, "SPY", workup_payload=payload)
    d1 = fe.persist_feature_snapshot(fr, payload["contracts"], "2026-07-27", path=path)
    d2 = fe.persist_feature_snapshot(fr, payload["contracts"], "2026-07-27", path=path)
    check("first write returns decision_id", d1 is not None)
    check("second write in same minute is a no-op", d2 is None)
    lines = open(path).read().strip().split("\n")
    check("exactly one snapshot line", len(lines) == 1)
    snap = json.loads(lines[0])
    check("frozen contract universe present", len(snap["contract_universe"]) == 1)
    check("feature_snapshot_hash present", bool(snap["feature_snapshot_hash"]))
    check("contract_snapshot_hash present",
          bool(snap["contract_universe"][0]["contract_snapshot_hash"]))


# ── PENDING until horizon complete ─────────────────────────────────────────
def test_pending_until_horizon():
    print("Label is PENDING while horizon not filled and no hit and not expired")
    _reset_labels()
    _write_snapshot([_contract()], horizon=3)
    prov = lambda t, dts, now: [_bar(1, 100.1, 99.9, 100.0),
                                _bar(2, 100.1, 99.9, 100.0)]   # 2 bars < horizon 3, no hit
    s = ll.run_lifecycle(prov, now=NOW, snapshots_path=SNAPS, labels_path=LABELS)
    lab = ll.canonical_labels(LABELS)[0]
    check("status PENDING", lab["status"] == ll.PENDING)
    check("summary counts one pending", s["labels_pending"] == 1)
    check("proxy flags preserved", lab["label_basis"] == "delta_gamma_proxy"
          and lab["realized_option_path"] is False)


def test_final_on_fill_no_hit():
    print("Label FINAL (no_hit) once horizon bars are filled")
    _reset_labels()
    _write_snapshot([_contract()], horizon=3)
    prov = lambda t, dts, now: [_bar(1, 100.1, 99.9, 100.0), _bar(2, 100.1, 99.9, 100.0),
                                _bar(3, 100.1, 99.9, 100.0)]
    s = ll.run_lifecycle(prov, now=NOW, snapshots_path=SNAPS, labels_path=LABELS)
    lab = ll.canonical_labels(LABELS)[0]
    check("status FINAL", lab["status"] == ll.FINAL)
    check("collision_result no_hit", lab["collision_result"] == "no_hit")
    check("valid_bars == 3", lab["valid_bars"] == 3)
    check("exit_reason horizon_end", lab["exit_reason"] == "horizon_end")


def test_final_target_hit_no_leak():
    print("A forward target hit finalizes as target; only forward bars used")
    _reset_labels()
    _write_snapshot([_contract()], horizon=5)
    # first forward bar hits target (underlying +0.6 -> option +0.30 >= +25%)
    prov = lambda t, dts, now: [_bar(1, 100.6, 99.9, 100.5), _bar(2, 100.7, 100.0, 100.6)]
    ll.run_lifecycle(prov, now=NOW, snapshots_path=SNAPS, labels_path=LABELS)
    lab = ll.canonical_labels(LABELS)[0]
    check("target_before_stop True", lab["target_before_stop"] is True)
    check("first_hit target", lab["first_hit"] == "target")
    check("minutes_to_first_hit == 1", lab["minutes_to_first_hit"] == 1)
    check("first_target_hit_ts set", lab["first_target_hit_ts"] is not None)
    check("terminal_timestamp set", lab["terminal_timestamp"] is not None)


def test_unlabelable_expired_no_bars():
    print("Expired contract with no forward bars is UNLABELABLE (not fabricated)")
    _reset_labels()
    _write_snapshot([_contract(expiry="2026-07-27")], horizon=3)
    prov = lambda t, dts, now: []            # no bars ever
    ll.run_lifecycle(prov, now=NOW_AFTER_EXPIRY, snapshots_path=SNAPS, labels_path=LABELS)
    lab = ll.canonical_labels(LABELS)[0]
    check("status UNLABELABLE", lab["status"] == ll.UNLABELABLE)
    check("reason names expiry", "expired" in (lab.get("unlabelable_reason") or ""))


def test_truncated_at_expiry_is_final():
    print("Expired contract with some bars but < horizon finalizes truncated")
    _reset_labels()
    _write_snapshot([_contract(expiry="2026-07-27")], horizon=10)
    prov = lambda t, dts, now: [_bar(1, 100.1, 99.9, 100.0), _bar(2, 100.1, 99.9, 100.0)]
    ll.run_lifecycle(prov, now=NOW_AFTER_EXPIRY, snapshots_path=SNAPS, labels_path=LABELS)
    lab = ll.canonical_labels(LABELS)[0]
    check("status FINAL", lab["status"] == ll.FINAL)
    check("truncated_at_expiry True", lab["truncated_at_expiry"] is True)


# ── idempotency + versioned correction ─────────────────────────────────────
def test_idempotent_noop_then_correction():
    print("Identical rerun is a no-op; a version bump with a different result corrects")
    _reset_labels()
    _write_snapshot([_contract()], horizon=3)
    prov = lambda t, dts, now: [_bar(1, 100.6, 99.9, 100.5)]     # target hit -> FINAL
    ll.run_lifecycle(prov, now=NOW, snapshots_path=SNAPS, labels_path=LABELS)
    n1 = len(open(LABELS).read().strip().split("\n"))
    s2 = ll.run_lifecycle(prov, now=NOW, snapshots_path=SNAPS, labels_path=LABELS)
    n2 = len(open(LABELS).read().strip().split("\n"))
    check("second identical run writes nothing new", n2 == n1)
    check("second run reports a no-op", s2["noops"] >= 1 and s2["labels_finalized"] == 0)

    # version bump → recompute → correction record (both preserved, hashes recorded)
    old = ll.OUTCOME_ENGINE_VERSION
    ll.OUTCOME_ENGINE_VERSION = "label-lifecycle-v0-test-bump"
    try:
        s3 = ll.run_lifecycle(prov, now=NOW, snapshots_path=SNAPS, labels_path=LABELS)
    finally:
        ll.OUTCOME_ENGINE_VERSION = old
    n3 = len(open(LABELS).read().strip().split("\n"))
    check("correction appended (old record preserved)", n3 == n2 + 1)
    check("summary reports a correction", s3["corrections"] == 1)
    corr = ll.canonical_labels(LABELS)[0]
    check("correction flagged", corr["is_correction"] is True)
    check("prior + new hashes recorded",
          corr.get("prior_label_hash") and corr.get("new_label_hash")
          and corr["prior_label_hash"] != corr["new_label_hash"])
    check("correction reason present", bool(corr.get("correction_reason")))


# ── failure isolation ──────────────────────────────────────────────────────
def test_failure_isolation_per_contract():
    print("A bad contract records ERROR_RETRYABLE; the good one still finalizes")
    _reset_labels()
    good = _contract(sym="GOOD")
    bad = _contract(sym="BAD")
    bad["mid"] = "not_a_number"              # forces a compute error for this one only
    _write_snapshot([good, bad], horizon=3)
    prov = lambda t, dts, now: [_bar(1, 100.6, 99.9, 100.5)]    # good -> target FINAL
    s = ll.run_lifecycle(prov, now=NOW, snapshots_path=SNAPS, labels_path=LABELS)
    labs = {l["contract_symbol"]: l for l in ll.canonical_labels(LABELS)}
    check("good contract finalized", labs["GOOD"]["status"] == ll.FINAL)
    check("bad contract ERROR_RETRYABLE", labs["BAD"]["status"] == ll.ERROR_RETRYABLE)
    check("run did not abort (both examined)", s["contracts_examined"] == 2)
    check("error + retry counted", s["errors"] >= 1 and s["retries"] >= 1)


def test_provider_failure_is_retryable():
    print("A bars_provider failure marks contracts ERROR_RETRYABLE, run continues")
    _reset_labels()
    _write_snapshot([_contract()], horizon=3)
    def boom(t, dts, now):
        raise RuntimeError("data feed down")
    s = ll.run_lifecycle(boom, now=NOW, snapshots_path=SNAPS, labels_path=LABELS)
    lab = ll.canonical_labels(LABELS)[0]
    check("status ERROR_RETRYABLE", lab["status"] == ll.ERROR_RETRYABLE)
    check("error_reason mentions provider", "provider" in lab.get("error_reason", ""))
    check("summary counted a retry", s["retries"] >= 1)


# ── required field completeness ────────────────────────────────────────────
def test_required_fields_present():
    print("A FINAL label carries the full required field set")
    _reset_labels()
    _write_snapshot([_contract()], horizon=3)
    prov = lambda t, dts, now: [_bar(1, 100.6, 99.9, 100.5)]
    ll.run_lifecycle(prov, now=NOW, snapshots_path=SNAPS, labels_path=LABELS)
    lab = ll.canonical_labels(LABELS)[0]
    required = ["forward_window_start", "forward_window_end", "valid_bars",
                "first_target_hit_ts", "first_stop_hit_ts", "collision_result",
                "mfe_pct", "mae_pct", "terminal_timestamp", "unlabelable_reason",
                "feature_snapshot_hash", "contract_snapshot_hash",
                "label_policy_version", "outcome_engine_version", "label_hash",
                "canonical_key", "calculation_timestamp", "label_basis",
                "realized_option_path", "formal_cohort_eligible"]
    missing = [k for k in required if k not in lab]
    check(f"all required fields present (missing={missing})", not missing)
    check("non-qualifying", lab["formal_cohort_eligible"] is False)


if __name__ == "__main__":
    for fn in (test_snapshot_immutable_idempotent, test_pending_until_horizon,
               test_final_on_fill_no_hit, test_final_target_hit_no_leak,
               test_unlabelable_expired_no_bars, test_truncated_at_expiry_is_final,
               test_idempotent_noop_then_correction, test_failure_isolation_per_contract,
               test_provider_failure_is_retryable, test_required_fields_present):
        fn()
    print("\nALL LABEL LIFECYCLE TESTS PASSED")
