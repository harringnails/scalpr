"""
INTEL VALIDATION REPORT (`scalpr-intel-v0`) — operational, non-qualifying.

Reads the Phase 1 pipeline artifacts and computes the operational metrics the
audit asked for, grouped by session. This is an OPERATIONAL health report — it
verifies the data-generation machinery (dedup, freezing, labeling, quote
quality, collisions), NOT profitability. `formal_cohort_eligible` is False; no
edge is claimed.

Metrics: snapshots created · duplicate snapshots prevented · contracts frozen ·
contracts by label state · median & max pending age · missing-data rates by
feature · stale-quote rates · lifecycle errors · proxy target/stop collisions ·
score-decision distribution.

CLI: python3 intel_validation_report.py  → writes INTEL_VALIDATION_REPORT.md
"""
import json
from collections import Counter
from datetime import datetime, timezone

import feature_engine as fe
import label_lifecycle as ll

TARGET_SESSIONS = 7
_META_KEYS = {"data_status", "_unwired"}


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def build_report(snapshots_path=fe.FEATURE_SNAPSHOTS_LOG,
                 dedup_path=fe.SNAPSHOT_DEDUP_LOG,
                 labels_path=ll.LIFECYCLE_LABELS_LOG, now=None):
    now = now or datetime.now(timezone.utc)
    snaps = fe.read_feature_snapshots(snapshots_path)
    dedups = list(fe._iter_jsonl(dedup_path))
    labels = ll.canonical_labels(labels_path)

    sessions = sorted({s.get("market_date") for s in snaps})
    contracts_frozen = sum(len(s.get("contract_universe", [])) for s in snaps)

    # label states
    states = Counter(l.get("status") for l in labels)

    # pending age (hours) from decision_timestamp → now
    pend_ages = []
    for l in labels:
        if l.get("status") == ll.PENDING:
            dt = _parse(l.get("decision_timestamp"))
            if dt:
                pend_ages.append((now - dt).total_seconds() / 3600.0)
    pend_ages.sort()

    def _median(xs):
        if not xs:
            return None
        m = len(xs) // 2
        return round(xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2, 3)

    # missing-data rates by feature field (fraction of snapshots where null)
    field_null = Counter()
    field_seen = Counter()
    for s in snaps:
        fr = s.get("feature_record", {})
        for grp in ("market_regime", "underlying", "options_flow", "volatility",
                    "events", "liquidity"):
            g = fr.get(grp, {})
            if not isinstance(g, dict):
                continue
            for k, v in g.items():
                if k in _META_KEYS:
                    continue
                field_seen[f"{grp}.{k}"] += 1
                if v is None:
                    field_null[f"{grp}.{k}"] += 1
    missing_rates = {k: round(field_null[k] / field_seen[k], 3)
                     for k in field_seen if field_null[k]}

    # stale-quote + bad-quote rates across all frozen contracts
    q_total = q_stale = q_bad = 0
    for s in snaps:
        for c in s.get("contract_universe", []):
            q_total += 1
            if c.get("quote_quality_warning") == "quote_stale":
                q_stale += 1
            if c.get("quote_quality") in ("crossed", "missing", "unusable", "locked"):
                q_bad += 1

    # lifecycle errors + collisions from canonical labels
    errors = states.get(ll.ERROR_RETRYABLE, 0)
    collisions = sum(1 for l in labels if l.get("collision_result") == "ambiguous_same_bar")

    # label OUTCOMES separated by quote-quality bucket (high / warning / locked /
    # stale-unlabelable / unusable-unlabelable). For each bucket: state counts +
    # target_before_stop tally among FINAL labels.
    BUCKETS = ("high_quality", "warning_quality", "locked", "stale_unlabelable",
               "unusable_unlabelable")
    by_bucket = {b: {"count": 0, "by_status": Counter(),
                     "final_target_before_stop": 0, "final_total": 0} for b in BUCKETS}
    for l in labels:
        b = l.get("quote_bucket") or "high_quality"
        slot = by_bucket.setdefault(
            b, {"count": 0, "by_status": Counter(),
                "final_target_before_stop": 0, "final_total": 0})
        slot["count"] += 1
        slot["by_status"][l.get("status")] += 1
        if l.get("status") == ll.FINAL:
            slot["final_total"] += 1
            if l.get("target_before_stop"):
                slot["final_target_before_stop"] += 1
    by_bucket = {b: {**v, "by_status": dict(v["by_status"])} for b, v in by_bucket.items()}

    # score-decision distribution (recompute deterministically from frozen records)
    decisions = Counter()
    for s in snaps:
        try:
            decisions[fe.score_record(s.get("feature_record", {})).get("decision")] += 1
        except Exception:
            decisions["score_error"] += 1

    return {
        "report_version": "scalpr-intel-v0",
        "generated_at": now.isoformat(),
        "formal_cohort_eligible": False,
        "evaluation_status": "operational_only",
        "sessions_collected": len(sessions),
        "sessions_target": TARGET_SESSIONS,
        "sessions": sessions,
        "snapshots_created": len(snaps),
        "duplicate_snapshots_prevented": len(dedups),
        "contracts_frozen": contracts_frozen,
        "contracts_by_label_state": dict(states),
        "pending_age_hours": {"median": _median(pend_ages),
                              "max": round(pend_ages[-1], 3) if pend_ages else None,
                              "count": len(pend_ages)},
        "missing_data_rates_by_feature": dict(sorted(missing_rates.items(),
                                              key=lambda kv: -kv[1])),
        "quote_quality": {"contracts": q_total,
                          "stale_rate": round(q_stale / q_total, 3) if q_total else None,
                          "bad_or_locked_rate": round(q_bad / q_total, 3) if q_total else None},
        "lifecycle_errors": errors,
        "proxy_target_stop_collisions": collisions,
        "label_outcomes_by_quote_bucket": by_bucket,
        "score_decision_distribution": dict(decisions),
        "note": ("Operational health only — verifies the data-generation machinery, "
                 "not profitability or edge. Delta-gamma proxy labels; no ML; no "
                 "calibrated probabilities. Non-qualifying (formal_cohort_eligible=false)."),
    }


def render_markdown(r):
    L = ["# Scalpr Intelligence — Phase 1 Validation Report",
         "",
         f"*Generated {r['generated_at']} · `{r['report_version']}` · "
         f"**operational only**, non-qualifying (no ML, no probabilities, no edge claim).*",
         "",
         f"**Sessions collected: {r['sessions_collected']} / {r['sessions_target']}**"
         + ("  — *awaiting live sessions; metrics below are the current (near-empty) "
            "state of a freshly-activated pipeline.*" if r["sessions_collected"] < r["sessions_target"] else ""),
         ""]
    if r["sessions"]:
        L.append("Sessions: " + ", ".join(str(s) for s in r["sessions"]) + "\n")
    L += ["## Pipeline volume", "",
          f"- Snapshots created: **{r['snapshots_created']}**",
          f"- Duplicate snapshots prevented: **{r['duplicate_snapshots_prevented']}**",
          f"- Contracts frozen (complete in-band universe): **{r['contracts_frozen']}**",
          "", "## Label states", ""]
    if r["contracts_by_label_state"]:
        for k, v in r["contracts_by_label_state"].items():
            L.append(f"- {k}: **{v}**")
    else:
        L.append("- (no labels yet)")
    pa = r["pending_age_hours"]
    L += ["", "## Pending age (hours)", "",
          f"- median: **{pa['median']}** · max: **{pa['max']}** · pending labels: {pa['count']}",
          "", "## Quote quality", ""]
    q = r["quote_quality"]
    L += [f"- contracts assessed: **{q['contracts']}**",
          f"- stale-quote rate: **{q['stale_rate']}** · bad/locked-quote rate: **{q['bad_or_locked_rate']}**",
          "", "## Missing-data rates by feature (null fraction across snapshots)", ""]
    if r["missing_data_rates_by_feature"]:
        for k, v in r["missing_data_rates_by_feature"].items():
            L.append(f"- {k}: **{v}**")
    else:
        L.append("- (no snapshots yet)")
    L += ["", "## Label outcomes by quote-quality bucket", "",
          "| bucket | contracts | FINAL | target-before-stop | other states |",
          "|---|--:|--:|--:|---|"]
    for b, v in r["label_outcomes_by_quote_bucket"].items():
        others = {k: n for k, n in v["by_status"].items() if k != "FINAL"}
        tbs = (f"{v['final_target_before_stop']}/{v['final_total']}"
               if v["final_total"] else "—")
        L.append(f"| {b} | {v['count']} | {v['by_status'].get('FINAL', 0)} | {tbs} | "
                 f"{others or '—'} |")
    L += ["", "*high_quality = clean quote · warning_quality = minor-stale (>15m) but "
          "labelable · locked = bid==ask · stale_unlabelable = materially stale "
          "(>60m) → UNLABELABLE_STALE · unusable_unlabelable = crossed/missing/"
          "unusable. Contracts stay frozen in the universe even when unlabelable.*",
          "", "## Lifecycle health", "",
          f"- lifecycle errors (ERROR_RETRYABLE): **{r['lifecycle_errors']}**",
          f"- proxy target/stop collisions (ambiguous_same_bar): **{r['proxy_target_stop_collisions']}**",
          "", "## Rules-score decision distribution", ""]
    if r["score_decision_distribution"]:
        for k, v in r["score_decision_distribution"].items():
            L.append(f"- {k}: **{v}**")
    else:
        L.append("- (no snapshots yet)")
    L += ["", "---", "", r["note"], ""]
    return "\n".join(L)


if __name__ == "__main__":
    rep = build_report()
    with open("INTEL_VALIDATION_REPORT.md", "w") as f:
        f.write(render_markdown(rep))
    print(json.dumps(rep, indent=2))
