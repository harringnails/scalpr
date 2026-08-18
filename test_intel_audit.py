"""
Production-readiness AUDIT tests for the Phase 1 intel pipeline.

Covers the ten audit concerns that the base suites don't already pin down:
  1  full in-band universe coverage (no [:N] / top-N / affordable cap)
  2  version-scoped snapshot identity + same-minute dedup + dedup logging
  3  atomic append + corrupt/incomplete line tolerated on read
  4  (calendar handling is exercised by the injected provider; horizon/expiry
     logic here is calendar-agnostic and tested in the lifecycle suite)
  5  collision policy: same-bar target+stop is conservative, never a win
  6  proxy math boundaries: deep-ITM / ATM / low-delta / negative clamp / puts
  7  quote quality: crossed/missing/unusable -> UNLABELABLE; locked/stale warn
  8  score reproducibility from a persisted snapshot, no live APIs
  9  failure recovery: corrupt snapshot among valid, malformed contract,
     missed-session finalize, duplicate invocation, restart-resume

Run: python3 test_intel_audit.py
"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import feature_engine as fe
import label_lifecycle as ll


def _shift(ts, secs):
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (d - timedelta(seconds=secs)).isoformat()

TMP = tempfile.mkdtemp()
DTS = "2026-07-27T14:00:00+00:00"
FAR = "2026-08-21"
NOW = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok: {name}")


def _bar(minute, hi, lo, cl, date="2026-07-27"):
    return {"ts": f"{date}T14:{minute:02d}:00+00:00", "high": hi, "low": lo, "close": cl}


def _contract(sym, mid=1.0, delta=0.5, gamma=0.0, typ="C", expiry=FAR,
              bid=0.98, ask=1.02, spread_pct=4.0):
    fq = {k: None for k in fe.FROZEN_QUOTE_FIELDS}
    fq.update({"symbol": sym, "mid": mid, "delta": delta, "gamma": gamma, "type": typ,
               "expiry": expiry, "bid": bid, "ask": ask, "spread_pct": spread_pct})
    return fq


# 1 ── full in-band universe coverage ──────────────────────────────────────
def test_full_universe_no_cap():
    print("Snapshot freezes EVERY in-band contract — no top-N / affordable cap")
    deltas = [0.05, 0.10, 0.15, 0.22, 0.30, 0.45, 0.50, 0.60, 0.70, 0.80, 0.95,
              -0.16, -0.35, -0.68, -0.75]
    contracts = [_contract(f"S{i}", delta=d) for i, d in enumerate(deltas)]
    expected = sum(1 for d in deltas if 0.15 <= abs(d) <= 0.70)   # = 10
    fr = {"ticker": "SPY", "timestamp": DTS, "underlying": {"last": 100.0}}
    path = os.path.join(TMP, "u.jsonl")
    fe.persist_feature_snapshot(fr, contracts, "2026-07-27", path=path)
    snap = fe.read_feature_snapshots(path)[0]
    check(f"universe holds all {expected} in-band contracts", len(snap["contract_universe"]) == expected)
    frozen = {c["symbol"] for c in snap["contract_universe"]}
    check("excludes out-of-band only", all(0.15 <= abs(d) <= 0.70
          for d, c in zip(deltas, contracts) if c["symbol"] in frozen))


# 2 ── snapshot identity + dedup ───────────────────────────────────────────
def test_identity_and_dedup():
    print("One snapshot per ticker/minute/version; re-fire deduped + logged")
    path = os.path.join(TMP, "id.jsonl")
    dedup = os.path.join(TMP, "dedup.jsonl")
    fe.SNAPSHOT_DEDUP_LOG = dedup
    fr = {"ticker": "SPY", "timestamp": DTS, "underlying": {"last": 100.0}}
    d1 = fe.persist_feature_snapshot(fr, [_contract("A")], "2026-07-27", path=path)
    d2 = fe.persist_feature_snapshot(fr, [_contract("A")], "2026-07-27", path=path)
    check("decision_id is version-scoped", fe.SCHEMA_VERSION in d1
          and fe.CONTRACT_UNIVERSE_VERSION in d1 and fe.RULES_SCORE_VERSION in d1)
    check("same-minute re-fire deduped", d2 is None)
    check("only one snapshot line", len(fe.read_feature_snapshots(path)) == 1)
    check("dedup event logged", os.path.exists(dedup)
          and len(open(dedup).read().strip().split("\n")) == 1)
    # a schema-version change produces a NEW (non-colliding) identity
    old = fe.SCHEMA_VERSION
    fe.SCHEMA_VERSION = "scalpr-intel-vNEXT"
    try:
        d3 = fe.persist_feature_snapshot(fr, [_contract("A")], "2026-07-27", path=path)
    finally:
        fe.SCHEMA_VERSION = old
    check("version change does not collide with old snapshot", d3 is not None)
    check("both versions coexist", len(fe.read_feature_snapshots(path)) == 2)


# 3 ── atomic append + tolerant read ───────────────────────────────────────
def test_atomic_append_and_tolerant_read():
    print("Atomic append writes whole lines; reader skips a corrupt/partial line")
    path = os.path.join(TMP, "atom.jsonl")
    fe._atomic_append(path, {"n": 1})
    fe._atomic_append(path, {"n": 2})
    with open(path, "a") as f:
        f.write('{"n": 3, PARTIAL')            # simulate a crash-truncated line
    fe._atomic_append(path, {"n": 4})
    got = [r["n"] for r in fe._iter_jsonl(path)]
    check("valid records survive, corrupt line skipped", got == [1, 2, 4])


# 5 ── collision policy ────────────────────────────────────────────────────
def test_collision_conservative():
    print("Same-bar target+stop is ambiguous, resolved stop_first, never a win")
    snap = _mk_snap([_contract("C", mid=1.0, delta=0.5, gamma=0.0)], horizon=3)
    # one bar whose high hits +25% target AND low hits -20% stop simultaneously
    bar = {"ts": f"{DTS[:10]}T14:01:00+00:00", "high": 100.6, "low": 99.5, "close": 100.0}
    lab = ll.compute_contract_label(snap, snap["contract_universe"][0], [bar], now=NOW)
    check("collision_result ambiguous_same_bar", lab["collision_result"] == "ambiguous_same_bar")
    check("counts_as_target_win False", lab["counts_as_target_win"] is False)
    check("target_before_stop False", lab["target_before_stop"] is False)
    check("stop_first: stop_before_target True", lab["stop_before_target"] is True)
    check("policy recorded", lab["collision_policy"] == ll.COLLISION_POLICY == "stop_first")


# 6 ── proxy math boundaries ───────────────────────────────────────────────
def test_proxy_boundaries():
    print("Proxy: deep-ITM/ATM/low-delta scale correctly; clamps >=0; puts sign")
    P = fe._proxy_option_price
    check("ATM call +1 (d0.5,g0.02): 1.51", abs(P(1.0, 0.5, 0.02, 1.0) - 1.51) < 1e-9)
    check("deep-ITM call +1 (d0.9,g0.01): 1.905", abs(P(1.0, 0.9, 0.01, 1.0) - 1.905) < 1e-9)
    check("low-delta call +1 (d0.15): 1.15", abs(P(1.0, 0.15, 0.0, 1.0) - 1.15) < 1e-9)
    check("large adverse move clamps to 0 (never negative)", P(1.0, 0.5, 0.0, -3.0) == 0.0)
    check("put gains on down move (d-0.5, du-1): 1.5", abs(P(1.0, -0.5, 0.0, -1.0) - 1.5) < 1e-9)


# 7 ── quote quality ───────────────────────────────────────────────────────
def test_quote_quality_gating():
    print("Crossed/missing/unusable -> UNLABELABLE; locked/stale -> warn but label")
    # crossed
    crossed = _contract("X", bid=1.10, ask=1.00, mid=1.05)   # ask < bid
    q, w, _ = fe.assess_quote_quality(crossed)
    check("crossed detected", q == "crossed")
    snap = _mk_snap([{**crossed, "quote_quality": q, "quote_quality_warning": w,
                      "contract_snapshot_hash": "h"}], horizon=3)
    lab = ll.compute_contract_label(snap, snap["contract_universe"][0],
                                    [_bar(1, 100.6, 99.9, 100.5)], now=NOW)
    check("crossed -> UNLABELABLE", lab["status"] == ll.UNLABELABLE
          and "crossed" in lab["unlabelable_reason"])
    # stale (old workup pull) still labelable, warning carried
    q2, w2, age = fe.assess_quote_quality(_contract("Y"), decision_ts=DTS,
                                          quote_as_of="2026-07-27T13:00:00+00:00")
    check("stale flagged (age 1h > 15m)", w2 == "quote_stale" and age == 3600)
    check("stale is still 'ok' quality (labelable)", q2 == "ok")
    # locked
    q3, w3, _ = fe.assess_quote_quality(_contract("Z", bid=1.0, ask=1.0, mid=1.0))
    check("locked detected", q3 == "locked" and w3 == "quote_locked")


# 8 ── score reproducibility from persisted snapshot ───────────────────────
def test_score_reproducible_from_disk():
    print("Rules score reproduces field-for-field from a persisted snapshot, no APIs")
    payload = {"spot": 100.0, "iv_rank": 30,
               "contracts": [_contract("A", delta=0.5), _contract("B", delta=0.3)]}
    # This test is about persisted-score determinism, not fitting the machine's
    # potentially large local regime history.
    with patch("regime_model.regime_read", return_value={"available": False}):
        fr = fe.build_feature_record(None, "SPY", workup_payload=payload)
    path = os.path.join(TMP, "score.jsonl")
    fe.persist_feature_snapshot(fr, payload["contracts"], "2026-07-27", path=path)
    snap = fe.read_feature_snapshots(path)[0]
    s1 = fe.score_record(snap["feature_record"])
    s2 = fe.score_record(snap["feature_record"])
    for k in ("direction", "direction_score", "trade_quality_score",
              "executability_score", "decision", "vetoes", "evidence"):
        check(f"'{k}' reproduces exactly", fe.canonical_hash(s1[k]) == fe.canonical_hash(s2[k]))


# 9 ── failure recovery ────────────────────────────────────────────────────
def _mk_snap(universe, horizon=3, u0=100.0, decision_ts=DTS):
    for c in universe:
        c.setdefault("contract_snapshot_hash", fe.canonical_hash(c))
    fr = {"ticker": "SPY", "timestamp": decision_ts}
    return {"decision_id": f"SPY:{decision_ts[:10]}:1400:{fe.SCHEMA_VERSION}",
            "schema_version": fe.SCHEMA_VERSION, "ticker": "SPY",
            "market_date": decision_ts[:10], "decision_timestamp": decision_ts,
            "underlying_at_decision": u0, "label_horizon_min": horizon,
            "label_target_pct": fe.LABEL_TARGET_PCT, "label_stop_pct": fe.LABEL_STOP_PCT,
            "feature_snapshot_hash": fe.canonical_hash(fr), "feature_record": fr,
            "contract_universe": universe}


def test_corrupt_snapshot_among_valid():
    print("A corrupt snapshot line is skipped; valid snapshots still process")
    snaps = os.path.join(TMP, "rec_s.jsonl"); labels = os.path.join(TMP, "rec_l.jsonl")
    with open(snaps, "w") as f:
        f.write("{ this is not json }\n")
        f.write(json.dumps(_mk_snap([_contract("GOOD")])) + "\n")
    prov = lambda t, dts, now: [_bar(1, 100.6, 99.9, 100.5)]
    s = ll.run_lifecycle(prov, now=NOW, snapshots_path=snaps, labels_path=labels)
    check("valid snapshot processed despite corrupt neighbor", s["snapshots_examined"] == 1)
    check("good contract finalized", ll.canonical_labels(labels)[0]["status"] == ll.FINAL)


def test_malformed_contract_isolated():
    print("A malformed contract record is ERROR_RETRYABLE; run continues")
    snaps = os.path.join(TMP, "mc_s.jsonl"); labels = os.path.join(TMP, "mc_l.jsonl")
    bad = _contract("BAD"); bad["mid"] = "NaN-ish"
    with open(snaps, "w") as f:
        f.write(json.dumps(_mk_snap([_contract("OK"), bad])) + "\n")
    prov = lambda t, dts, now: [_bar(1, 100.6, 99.9, 100.5)]
    s = ll.run_lifecycle(prov, now=NOW, snapshots_path=snaps, labels_path=labels)
    labs = {l["contract_symbol"]: l for l in ll.canonical_labels(labels)}
    check("OK finalized", labs["OK"]["status"] == ll.FINAL)
    check("BAD isolated as ERROR_RETRYABLE", labs["BAD"]["status"] == ll.ERROR_RETRYABLE)


def test_missed_sessions_then_finalize_and_resume():
    print("Missed-session PENDING finalizes on a later run; duplicate run is a no-op")
    snaps = os.path.join(TMP, "ms_s.jsonl"); labels = os.path.join(TMP, "ms_l.jsonl")
    with open(snaps, "w") as f:
        f.write(json.dumps(_mk_snap([_contract("M")], horizon=5)) + "\n")
    # first close: only 2 forward bars -> PENDING
    ll.run_lifecycle(lambda t, d, n: [_bar(1, 100.1, 99.9, 100.0), _bar(2, 100.1, 99.9, 100.0)],
                     now=NOW, snapshots_path=snaps, labels_path=labels)
    check("pending after partial", ll.canonical_labels(labels)[0]["status"] == ll.PENDING)
    # a later close (simulated restart: fresh read from disk) with full bars -> FINAL
    full = [_bar(m, 100.1, 99.9, 100.0) for m in range(1, 6)]
    ll.run_lifecycle(lambda t, d, n: full, now=NOW, snapshots_path=snaps, labels_path=labels)
    check("finalized after horizon fills (resume from disk)",
          ll.canonical_labels(labels)[0]["status"] == ll.FINAL)
    # duplicate invocation: no-op
    s = ll.run_lifecycle(lambda t, d, n: full, now=NOW, snapshots_path=snaps, labels_path=labels)
    check("duplicate lifecycle run is a no-op", s["labels_finalized"] == 0 and s["noops"] >= 1)


def test_multi_session_window():
    print("Forward bars spanning two sessions accumulate toward the horizon")
    snaps = os.path.join(TMP, "mss_s.jsonl"); labels = os.path.join(TMP, "mss_l.jsonl")
    with open(snaps, "w") as f:
        f.write(json.dumps(_mk_snap([_contract("MS")], horizon=4)) + "\n")
    two_day = [_bar(1, 100.1, 99.9, 100.0, "2026-07-27"),
               _bar(2, 100.1, 99.9, 100.0, "2026-07-27"),
               _bar(1, 100.1, 99.9, 100.0, "2026-07-28"),
               _bar(2, 100.1, 99.9, 100.0, "2026-07-28")]
    later = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
    ll.run_lifecycle(lambda t, d, n: two_day, now=later, snapshots_path=snaps, labels_path=labels)
    lab = ll.canonical_labels(labels)[0]
    check("multi-session horizon filled -> FINAL", lab["status"] == ll.FINAL)
    check("valid_bars counts both sessions", lab["valid_bars"] == 4)


# 7b ── two-tier staleness ─────────────────────────────────────────────────
def test_two_tier_staleness_thresholds():
    print("Staleness thresholds: >15m warn (labelable) · >60m materially stale")
    c = _contract("S")
    q, w, a = fe.assess_quote_quality(c, DTS, _shift(DTS, 900))
    check("age 900s (15m) is NOT yet stale", w is None and a == 900)
    q, w, a = fe.assess_quote_quality(c, DTS, _shift(DTS, 960))
    check("age 960s -> minor-stale WARNING, still labelable", q == "ok" and w == "quote_stale")
    check("minor-stale bucket = warning_quality", fe.quote_bucket(q, w) == "warning_quality")
    q, w, a = fe.assess_quote_quality(c, DTS, _shift(DTS, 3600))
    check("age 3600s (60m) still only a warning", q == "ok" and w == "quote_stale")
    q, w, a = fe.assess_quote_quality(c, DTS, _shift(DTS, 3660))
    check("age 3660s (>60m) -> MATERIALLY stale", q == "stale" and w == "quote_materially_stale")
    check("materially-stale bucket = stale_unlabelable", fe.quote_bucket(q, w) == "stale_unlabelable")


def test_stale_unlabelable_preserved_in_universe():
    print("Materially-stale contract -> UNLABELABLE_STALE, still frozen in the universe")
    path = os.path.join(TMP, "stale.jsonl")
    fe.SNAPSHOT_DEDUP_LOG = os.path.join(TMP, "sdd.jsonl")
    fr = {"ticker": "SPY", "timestamp": DTS, "underlying": {"last": 100.0}}
    fe.persist_feature_snapshot(fr, [_contract("STALE")], "2026-07-27",
                                quote_as_of=_shift(DTS, 4000), path=path)
    snap = fe.read_feature_snapshots(path)[0]
    check("contract preserved in frozen universe",
          any(c["symbol"] == "STALE" for c in snap["contract_universe"]))
    c0 = snap["contract_universe"][0]
    check("frozen quote_quality == stale", c0["quote_quality"] == "stale")
    check("frozen quote_bucket == stale_unlabelable", c0["quote_bucket"] == "stale_unlabelable")
    labels = os.path.join(TMP, "stale_l.jsonl")
    s = ll.run_lifecycle(lambda t, dts, n: [_bar(1, 100.6, 99.9, 100.5)], now=NOW,
                         snapshots_path=path, labels_path=labels)
    lab = ll.canonical_labels(labels)[0]
    check("label status UNLABELABLE_STALE", lab["status"] == ll.UNLABELABLE_STALE)
    check("reason names material staleness", "materially_stale" in lab["unlabelable_reason"])
    check("bucket carried onto label", lab["quote_bucket"] == "stale_unlabelable")
    check("summary counts it separately", s["labels_unlabelable_stale"] == 1)


if __name__ == "__main__":
    for fn in (test_full_universe_no_cap, test_identity_and_dedup,
               test_atomic_append_and_tolerant_read, test_collision_conservative,
               test_proxy_boundaries, test_quote_quality_gating,
               test_two_tier_staleness_thresholds, test_stale_unlabelable_preserved_in_universe,
               test_score_reproducible_from_disk, test_corrupt_snapshot_among_valid,
               test_malformed_contract_isolated,
               test_missed_sessions_then_finalize_and_resume, test_multi_session_window):
        fn()
    print("\nALL INTEL AUDIT TESTS PASSED")
