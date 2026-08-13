"""IVolatility quote-fidelity + resolution probe (research annex, read-only).

Implements the §3 gate of IVOLATILITY_RESEARCH_ANNEX_PROPOSAL.md:

  1. Level fidelity   — how closely IVOL minute bid/ask matches a reference feed.
  2. Coverage/quality — how often IVOL's minute quote is zero-bid/one-sided/stale.
  3. Resolution       — how much first-hit (stop vs target) labels move MINUTE_1 vs 5.

This module performs NO order/broker/Guard action and imports none. It compares
two already-exported quote series and emits a stamped report. It is deliberately
pure-stdlib (json/csv/statistics/hashlib/datetime/zoneinfo) so it runs unchanged
in the IVolatility VS Code environment or the Scalpr host.

Two run stages (see annex §3 "Timing boundary"):
  * Stage P (proxy, PRELIMINARY): reference = Alpaca historical option quotes CSV.
        Can only FAIL the plan early; never clears the gate.
  * Stage D (definitive, GATE-CLEARING): reference = Scalpr capture JSONL
        (entry_intelligence_bid_ticks_v1.jsonl, status == "FRESH").

Nothing here converts a passing result into an edge claim; it only establishes
whether IVOL's historical quotes are faithful enough to build a pre-screen on.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:  # tz conversion is required; fail loudly rather than silently mis-align.
    from zoneinfo import ZoneInfo
except Exception as exc:  # pragma: no cover
    raise SystemExit("zoneinfo is required (Python 3.9+)") from exc


PROBE_VERSION = "ivol-fidelity-probe-v1"


# --------------------------------------------------------------------------- #
# Canonical hashing (matches the repo convention: sha256 over sorted, compact  #
# json with default=str) so a report is reproducible and citable.             #
# --------------------------------------------------------------------------- #
def canonical_hash(obj) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Time handling — the comparison frame is made EXPLICIT. Every source declares #
# its timestamp tz; everything is converted to UTC before flooring to a minute #
# (Codex gotcha: the repo mixes UTC and ET).                                   #
# --------------------------------------------------------------------------- #
def _parse_ts(value: str, assume_tz: str) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(assume_tz))
    return dt.astimezone(timezone.utc)


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _floor_bucket(dt: datetime, minutes: int) -> datetime:
    m = (dt.minute // minutes) * minutes
    return dt.replace(minute=m, second=0, microsecond=0)


# --------------------------------------------------------------------------- #
# Normalized quote row                                                         #
# --------------------------------------------------------------------------- #
@dataclass
class Quote:
    ts_utc: datetime
    bid: Optional[float]
    ask: Optional[float]
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None

    def usable(self) -> bool:
        return (
            self.bid is not None and self.ask is not None
            and self.bid > 0 and self.ask > 0 and self.ask >= self.bid
        )


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _last_per_minute(quotes: list[Quote]) -> dict[datetime, Quote]:
    """Collapse many observations to the LAST one in each UTC minute — the
    natural comparison to a minute-bar snapshot."""
    out: dict[datetime, Quote] = {}
    for q in sorted(quotes, key=lambda x: x.ts_utc):
        out[_floor_minute(q.ts_utc)] = q
    return out


# --------------------------------------------------------------------------- #
# Loaders                                                                       #
# --------------------------------------------------------------------------- #
def load_ivol_csv(path, assume_tz: str) -> list[Quote]:
    """IVOL minute chain exported from the IVOL env by ivol_pull_minute_chain.py.
    Expected columns: timestamp, option_symbol, bid, ask, bid_size, ask_size."""
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append(Quote(
                ts_utc=_parse_ts(r["timestamp"], assume_tz),
                bid=_num(r.get("bid")), ask=_num(r.get("ask")),
                bid_size=_num(r.get("bid_size")), ask_size=_num(r.get("ask_size")),
            ))
    return rows


def load_reference_csv(path, assume_tz: str) -> list[Quote]:
    """Alpaca historical option quotes exported to CSV (Stage P proxy).
    Expected columns: timestamp, bid, ask[, bid_size, ask_size]."""
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append(Quote(
                ts_utc=_parse_ts(r["timestamp"], assume_tz),
                bid=_num(r.get("bid")), ask=_num(r.get("ask")),
                bid_size=_num(r.get("bid_size")), ask_size=_num(r.get("ask_size")),
            ))
    return rows


def load_scalpr_capture_jsonl(path, option_symbol: str) -> list[Quote]:
    """Scalpr capture reference (Stage D). Reads entry_intelligence_bid_ticks_v1
    records and keeps only status == FRESH rows for the target contract.
    observed_at is already ISO/UTC-aware."""
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("option_symbol") != option_symbol:
                continue
            if rec.get("status") != "FRESH":
                continue
            rows.append(Quote(
                ts_utc=_parse_ts(rec["observed_at"], "UTC"),
                bid=_num(rec.get("bid")), ask=_num(rec.get("ask")),
            ))
    return rows


# --------------------------------------------------------------------------- #
# Read-off 1: level fidelity                                                    #
# --------------------------------------------------------------------------- #
def level_fidelity(ivol: dict[datetime, Quote], ref: dict[datetime, Quote],
                   tick: float, thresholds: dict) -> dict:
    shared = sorted(set(ivol) & set(ref))
    bid_abs, ask_abs, bid_signed, ask_signed = [], [], [], []
    for m in shared:
        a, b = ivol[m], ref[m]
        if not (a.usable() and b.usable()):
            continue
        bid_abs.append(abs(a.bid - b.bid)); bid_signed.append(a.bid - b.bid)
        ask_abs.append(abs(a.ask - b.ask)); ask_signed.append(a.ask - b.ask)

    def med(xs):
        return round(statistics.median(xs), 6) if xs else None

    n = len(bid_abs)
    result = {
        "shared_minutes": len(shared),
        "compared_minutes": n,
        "tick_size": tick,
        "median_abs_diff_bid": med(bid_abs),
        "median_abs_diff_ask": med(ask_abs),
        "median_abs_diff_bid_ticks": (round(med(bid_abs) / tick, 3) if bid_abs else None),
        "median_abs_diff_ask_ticks": (round(med(ask_abs) / tick, 3) if ask_abs else None),
        "median_signed_diff_bid": med(bid_signed),
        "median_signed_diff_ask": med(ask_signed),
    }
    max_abs = float(thresholds.get("max_median_abs_diff_ticks", 1.0)) * tick
    max_bias = float(thresholds.get("max_median_signed_bias_ticks", 0.5)) * tick
    min_cmp = int(thresholds.get("min_compared_minutes", 20))
    checks = {
        "enough_minutes": n >= min_cmp,
        "bid_level_ok": bool(bid_abs) and med(bid_abs) <= max_abs,
        "ask_level_ok": bool(ask_abs) and med(ask_abs) <= max_abs,
        "no_bid_bias": bool(bid_signed) and abs(med(bid_signed)) <= max_bias,
        "no_ask_bias": bool(ask_signed) and abs(med(ask_signed)) <= max_bias,
    }
    result["checks"] = checks
    result["pass"] = all(checks.values())
    return result


# --------------------------------------------------------------------------- #
# Read-off 2: coverage / quality of the IVOL feed itself                        #
# --------------------------------------------------------------------------- #
def coverage_quality(ivol_rows: list[Quote], thresholds: dict) -> dict:
    by_min = _last_per_minute(ivol_rows)
    total = len(by_min)
    usable = sum(1 for q in by_min.values() if q.usable())
    zero_bid = sum(1 for q in by_min.values() if (q.bid is None or q.bid <= 0))
    one_sided = sum(1 for q in by_min.values()
                    if (q.bid is None or q.bid <= 0) ^ (q.ask is None or q.ask <= 0))
    crossed = sum(1 for q in by_min.values()
                  if q.bid and q.ask and q.ask < q.bid)
    frac = round(usable / total, 6) if total else 0.0
    min_usable = float(thresholds.get("min_usable_minute_fraction", 0.90))
    return {
        "minutes": total,
        "usable_minutes": usable,
        "usable_fraction": frac,
        "zero_or_missing_bid_minutes": zero_bid,
        "one_sided_minutes": one_sided,
        "crossed_minutes": crossed,
        "pass": bool(total) and frac >= min_usable,
    }


# --------------------------------------------------------------------------- #
# Read-off 3: resolution sensitivity of first-hit labeling                      #
# --------------------------------------------------------------------------- #
def _first_hit(series: list[Quote], entry_ask: float, target_bid: float,
               stop_bid: float, start: datetime, horizon_min: int) -> Optional[str]:
    """Conservative first-hit: stop checked before target at the same bar
    (matches entry_bid_capture_v1.evaluate_outcome)."""
    end = start + timedelta(minutes=horizon_min)
    for q in sorted(series, key=lambda x: x.ts_utc):
        if not (start <= q.ts_utc <= end) or q.bid is None or q.bid <= 0:
            continue
        if q.bid <= stop_bid:
            return "STOP_FIRST"
        if q.bid >= target_bid:
            return "TARGET_FIRST"
    return "NO_HIT"


def _resample_bucket_last(rows: list[Quote], minutes: int) -> list[Quote]:
    buckets: dict[datetime, Quote] = {}
    for q in sorted(rows, key=lambda x: x.ts_utc):
        buckets[_floor_bucket(q.ts_utc, minutes)] = q
    return list(buckets.values())


def resolution_sensitivity(ivol_rows: list[Quote], instances: list[dict]) -> dict:
    """For each sample reversal instance, label first-hit at MINUTE_1 vs MINUTE_5
    and report agreement. Instances carry entry_ask/target_bid/stop_bid, an ISO
    start, horizon minutes, and the instance tz."""
    m1 = ivol_rows
    m5 = _resample_bucket_last(ivol_rows, 5)
    details = []
    agree = 0
    for inst in instances:
        start = _parse_ts(inst["start"], inst.get("tz", "UTC"))
        horizon = int(inst.get("horizon_minutes", 60))
        label1 = _first_hit(m1, float(inst["entry_ask"]), float(inst["target_bid"]),
                            float(inst["stop_bid"]), start, horizon)
        label5 = _first_hit(m5, float(inst["entry_ask"]), float(inst["target_bid"]),
                            float(inst["stop_bid"]), start, horizon)
        same = label1 == label5
        agree += int(same)
        details.append({
            "instance": inst.get("id"), "start_utc": start.isoformat(),
            "label_minute_1": label1, "label_minute_5": label5, "agree": same,
        })
    n = len(instances)
    return {
        "instances": n,
        "agreement": details,
        "agreement_fraction": round(agree / n, 6) if n else None,
        "pass": (n > 0 and agree == n),  # any disagreement is worth surfacing
        "note": "informational: disagreement quantifies the 1-min resolution risk",
    }


# --------------------------------------------------------------------------- #
# Orchestration                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class ProbeConfig:
    stage: str                      # "P" (proxy/preliminary) or "D" (definitive)
    option_symbol: str
    ivol_csv: str
    ivol_timestamp_tz: str          # MUST be confirmed for the IVOL feed
    reference_kind: str             # "alpaca_csv" | "scalpr_capture_jsonl"
    reference_path: str
    reference_timestamp_tz: str = "UTC"
    tick_size: float = 0.01
    thresholds: dict = field(default_factory=dict)
    resolution_instances: list = field(default_factory=list)


def run_probe(cfg: ProbeConfig) -> dict:
    ivol_rows = load_ivol_csv(cfg.ivol_csv, cfg.ivol_timestamp_tz)
    if cfg.reference_kind == "alpaca_csv":
        ref_rows = load_reference_csv(cfg.reference_path, cfg.reference_timestamp_tz)
    elif cfg.reference_kind == "scalpr_capture_jsonl":
        ref_rows = load_scalpr_capture_jsonl(cfg.reference_path, cfg.option_symbol)
    else:
        raise ValueError(f"unknown reference_kind: {cfg.reference_kind}")

    ivol_min = _last_per_minute(ivol_rows)
    ref_min = _last_per_minute(ref_rows)

    fidelity = level_fidelity(ivol_min, ref_min, cfg.tick_size, cfg.thresholds)
    coverage = coverage_quality(ivol_rows, cfg.thresholds)
    resolution = resolution_sensitivity(ivol_rows, cfg.resolution_instances)

    stage_is_definitive = cfg.stage.upper() == "D"
    # Stage P can only FAIL early; it can never clear the gate.
    gate_cleared = bool(
        stage_is_definitive and fidelity["pass"] and coverage["pass"]
    )
    early_warning = (not fidelity["pass"]) or (not coverage["pass"])

    config_record = {
        "probe_version": PROBE_VERSION,
        "stage": cfg.stage.upper(),
        "stage_label": ("DEFINITIVE_GATE_CLEARING" if stage_is_definitive
                        else "PRELIMINARY_PROXY_REFERENCE"),
        "option_symbol": cfg.option_symbol,
        "ivol_timestamp_tz": cfg.ivol_timestamp_tz,
        "reference_kind": cfg.reference_kind,
        "reference_timestamp_tz": cfg.reference_timestamp_tz,
        "tick_size": cfg.tick_size,
        "thresholds": cfg.thresholds,
    }
    report = {
        "probe_version": PROBE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": config_record,
        "config_hash": canonical_hash(config_record),
        "level_fidelity": fidelity,
        "coverage_quality": coverage,
        "resolution_sensitivity": resolution,
        "gate_cleared": gate_cleared,
        "early_warning": early_warning,
        "interpretation": (
            "Stage D definitive: gate cleared." if gate_cleared else
            "Stage D definitive: gate NOT cleared." if stage_is_definitive else
            "Stage P proxy: PRELIMINARY only — cannot clear the gate. "
            + ("Early-warning divergence flagged." if early_warning
               else "No early-warning divergence; still requires Stage D.")
        ),
        "is_edge_claim": False,
        "execution_authority": False,
    }
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IVolatility quote-fidelity probe")
    ap.add_argument("config", help="path to a probe config JSON")
    ap.add_argument("--out", help="path to write the JSON report", default=None)
    args = ap.parse_args(argv)

    raw = json.loads(Path(args.config).read_text())
    cfg = ProbeConfig(**raw)
    report = run_probe(cfg)

    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    # Exit non-zero on an early warning so a CI/dry-run notices divergence.
    return 1 if report["early_warning"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
