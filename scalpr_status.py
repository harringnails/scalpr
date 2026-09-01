"""Render a standalone read-only status page from existing Scalpr evidence.

Source files are opened for reading only. The sole write is an atomic replacement
of the requested HTML output. This module has no execution or admission authority.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable


MODULE_VERSION = "scalpr-readonly-status-v0"
DEFAULT_OUTPUT = "scalpr_status.html"
INFERENTIAL_BADGE = "INFERENTIAL · PROSPECTIVE ACCRUAL"
SHADOW_BADGE = "EXPLORATORY · NON-INFERENTIAL"
OPERATIONS_BADGE = "OPERATIONS · NON-SIGNAL"


@dataclass(frozen=True)
class Sources:
    root: Path
    pin_root: Path
    databento_root: Path


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except (FileNotFoundError, OSError):
        return []
    return rows


def _read_latest_log(path: Path) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    latest = {"message": text[-500:]}
                else:
                    latest = value if isinstance(value, dict) else {"message": str(value)}
    except (FileNotFoundError, OSError):
        return None
    return latest


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _deep(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _latest(rows: Iterable[dict[str, Any]], *fields: str) -> dict[str, Any] | None:
    ranked = []
    for index, row in enumerate(rows):
        stamp = None
        for field in fields:
            stamp = _parse_time(row.get(field))
            if stamp is not None:
                break
        ranked.append((stamp or datetime.min.replace(tzinfo=timezone.utc), index, row))
    return max(ranked, default=(None, None, None), key=lambda item: (item[0], item[1]))[2]


def _first_existing(paths: Iterable[Path], fallback: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return fallback


def discover_sources(
    root: Path,
    *,
    pin_root: Path | None = None,
    databento_root: Path | None = None,
) -> Sources:
    root = root.resolve()
    workspace = root.parent
    resolved_pin = pin_root or _first_existing(
        (root / "flashalpha_pin_study_v0.jsonl", workspace / "Scalpr7-flashalpha-pin-scanner"),
        root,
    )
    if resolved_pin.is_file():
        resolved_pin = resolved_pin.parent
    resolved_databento = databento_root or _first_existing(
        (
            root / "a2_exploratory_databento",
            workspace / "Scalpr7-a2-relabel" / "a2_exploratory_databento",
        ),
        root / "a2_exploratory_databento",
    )
    return Sources(root=root, pin_root=resolved_pin.resolve(), databento_root=resolved_databento.resolve())


def _a2_path(root: Path, name: str) -> Path:
    return _first_existing(
        (root / name, root / "v2_data" / "a2_measurement" / name),
        root / "v2_data" / "a2_measurement" / name,
    )


def reversal_model(sources: Sources) -> dict[str, Any]:
    summary = _read_json(_a2_path(sources.root, "a2_summary_dense_v0.json")) or {}
    accrual = _read_json(_a2_path(sources.root, "a2_accrual_status_v0.json")) or {}
    count = _integer(_deep(accrual, "gate", "non_overlapping_count"))
    if count is None:
        count = _integer(summary.get("clean_a2_eligible_episode_count"))
    target = _integer(_deep(accrual, "gate", "target")) or 200
    remaining = _integer(_deep(accrual, "gate", "remaining"))
    if remaining is None and count is not None:
        remaining = max(0, target - count)
    return {
        "available": bool(summary or accrual),
        "count": count,
        "target": target,
        "remaining": remaining,
        "gate_reached": _deep(accrual, "gate", "reached"),
        "phase": summary.get("phase4_preflight") or "not yet",
        "integrity": summary.get("data_integrity_status") or "—",
        "mean_signed_60m": _number(summary.get("mean_signed_return_60m")),
        "as_of": _deep(accrual, "accrual_window", "as_of_trading_session") or "—",
    }


def collector_model(sources: Sources, *, now: datetime) -> dict[str, Any]:
    status = _read_json(sources.root / "entry_intelligence_collector_status_v1.json") or {}
    updated = _parse_time(status.get("updated_at"))
    heartbeat_age = max(0.0, (now - updated).total_seconds()) if updated else None
    counts = status.get("last_session_counts")
    count_label = "last session"
    if not isinstance(counts, dict):
        counts = status.get("counters") if isinstance(status.get("counters"), dict) else {}
        count_label = "current counters"
    return {
        "available": bool(status),
        "state": status.get("state") or "—",
        "version": status.get("collector_version") or status.get("config_version") or "—",
        "heartbeat_age": heartbeat_age,
        "updated_at": status.get("updated_at") or "—",
        "execution_authority": status.get("execution_authority"),
        "guard_access": status.get("guard_access"),
        "cohorts_locked": status.get("cohorts_locked"),
        "collection_role": status.get("collection_role") or "—",
        "count_label": count_label,
        "counts": counts,
    }


def liveness_model(sources: Sources) -> dict[str, Any]:
    candidates = (
        sources.root / "collector_alerts_v0.log",
        sources.root / "collector_liveness_alert.out.log",
    )
    for path in candidates:
        latest = _read_latest_log(path)
        if latest:
            return {
                "available": True,
                "event": latest.get("event") or latest.get("status") or "—",
                "kind": latest.get("kind") or latest.get("exit") or "—",
                "message": latest.get("message") or "—",
                "state": latest.get("state") or "—",
                "at": latest.get("ts") or latest.get("at") or "—",
                "source": path.name,
            }
    return {"available": False, "event": "not yet", "kind": "—", "message": "—", "state": "—", "at": "—", "source": "—"}


def pin_model(sources: Sources) -> dict[str, Any]:
    rows = _read_jsonl(sources.pin_root / "flashalpha_pin_study_v0.jsonl")
    candidates = [row for row in rows if row.get("record_type") == "PIN_CANDIDATE"]
    outcomes = [
        row for row in rows
        if row.get("record_type") == "PIN_SESSION_OUTCOME" and row.get("status") == "AVAILABLE"
    ]
    latest = _latest(candidates, "observed_at_utc") or {}
    evidence = latest.get("evidence") if isinstance(latest.get("evidence"), dict) else {}
    pocket = evidence.get("pocket") if isinstance(evidence.get("pocket"), dict) else {}
    sessions = {str(row.get("session_date")) for row in outcomes if row.get("session_date")}
    return {
        "available": bool(latest),
        "grade": latest.get("grade") or "not yet",
        "observed_at": latest.get("observed_at_utc") or "—",
        "session_date": latest.get("session_date") or "—",
        "spot": _number(evidence.get("spot")),
        "pin_score": _number(evidence.get("pin_score")),
        "gamma_regime": evidence.get("gamma_regime") or "—",
        "put_wall": _number(pocket.get("put_wall")),
        "call_wall": _number(pocket.get("call_wall")),
        "spot_inside": pocket.get("spot_inside"),
        "available_days": len(sessions),
        "target_days": 20,
    }


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def iron_condor_model(sources: Sources) -> dict[str, Any]:
    rows = _read_jsonl(sources.pin_root / "flashalpha_pin_ic_study_v0.jsonl")
    settlements = [
        row for row in rows
        if row.get("record_type") == "IC_SETTLEMENT" and row.get("status") == "AVAILABLE"
    ]
    values = [
        value for row in settlements
        if (value := _number(_deep(row, "pnl", "net_pnl_dollars"))) is not None
    ]
    report = _read_json(sources.pin_root / "flashalpha_pin_ic_report_v0.json") or {}
    report_all = report.get("all_priceable_days") if isinstance(report.get("all_priceable_days"), dict) else {}
    return {
        "available": bool(values or report),
        "settled_days": _integer(report.get("available_days")) if report else len(values),
        "target_days": _integer(report.get("validation_target_available_days")) or 60,
        "mean_pnl": _number(report_all.get("mean_net_pnl_dollars")) if report_all else (mean(values) if values else None),
        "median_pnl": _number(report_all.get("median_net_pnl_dollars")) if report_all else (median(values) if values else None),
        "win_rate": _number(report_all.get("win_rate")) if report_all else (sum(value > 0 for value in values) / len(values) if values else None),
        "worst_day": _number(report_all.get("minimum_net_pnl_dollars")) if report_all else (min(values) if values else None),
        "p10": _number(report_all.get("p10_net_pnl_dollars")) if report_all else _quantile(values, 0.10),
        "bottom_decile": _number(report_all.get("bottom_decile_mean_net_pnl_dollars")),
        "expectancy": _number(report_all.get("net_expectancy_after_costs_dollars")) if report_all else (mean(values) if values else None),
    }


def _latest_file(root: Path, name: str) -> Path | None:
    try:
        matches = list(root.glob(f"**/{name}"))
    except OSError:
        return None
    if not matches:
        return None
    return max(matches, key=lambda path: (path.stat().st_mtime_ns, str(path)))


def databento_model(sources: Sources) -> dict[str, Any]:
    projection_path = _latest_file(sources.databento_root, "cost_projection.json")
    probe_path = _latest_file(sources.databento_root, "cost_probe.json")
    report_path = _latest_file(sources.databento_root, "exploratory_report.json")
    projection = _read_json(projection_path) if projection_path else None
    probe = _read_json(probe_path) if probe_path else None
    report = _read_json(report_path) if report_path else None
    projection, probe, report = projection or {}, probe or {}, report or {}
    episode_count = _integer(projection.get("episode_count"))
    if episode_count is None:
        episode_count = _integer(report.get("episode_count"))
    completed = bool(projection.get("cost_probe_completed") or probe)
    quote_projection = projection.get("quote_projection") if isinstance(projection.get("quote_projection"), dict) else {}
    return {
        "available": bool(projection or probe or report),
        "episode_count": episode_count,
        "window_count": _integer(projection.get("quote_window_count") or probe.get("total_window_count")),
        "probe_status": "complete" if completed else ("not yet" if projection else "—"),
        "sample_windows": _integer(probe.get("sample_window_count")),
        "actual_probe_cost": _number(probe.get("actual_probe_cost_usd")),
        "projected_quote_cost": _number(probe.get("projected_total_quote_cost_usd") or quote_projection.get("projected_total_quote_cost_usd")),
        "projected_quote_records": _integer(probe.get("projected_total_quote_records") or quote_projection.get("projected_total_quote_records")),
    }


def gather_status(sources: Sources, *, now: datetime) -> dict[str, Any]:
    now = now.astimezone(timezone.utc)
    return {
        "module_version": MODULE_VERSION,
        "generated_at_utc": now.isoformat(),
        "authority": {
            "read_only": True,
            "execution_authority": False,
            "admission_authority": False,
            "order_authority": False,
        },
        "reversal": reversal_model(sources),
        "collector": collector_model(sources, now=now),
        "liveness": liveness_model(sources),
        "pin": pin_model(sources),
        "iron_condor": iron_condor_model(sources),
        "databento": databento_model(sources),
    }


def _e(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return html.escape(str(value), quote=True)


def _fmt_int(value: Any) -> str:
    number = _integer(value)
    return f"{number:,}" if number is not None else "—"


def _fmt_money(value: Any) -> str:
    number = _number(value)
    return f"${number:,.2f}" if number is not None else "—"


def _fmt_pct(value: Any) -> str:
    number = _number(value)
    return f"{number * 100:.1f}%" if number is not None else "—"


def _fmt_price(value: Any) -> str:
    number = _number(value)
    return f"{number:,.2f}" if number is not None else "—"


def _fmt_age(seconds: Any) -> str:
    value = _number(seconds)
    if value is None:
        return "—"
    if value < 60:
        return f"{value:.0f}s"
    if value < 3600:
        return f"{value / 60:.1f}m"
    return f"{value / 3600:.1f}h"


def _bool_state(value: Any, *, true_text: str, false_text: str) -> str:
    if value is True:
        return true_text
    if value is False:
        return false_text
    return "—"


def _progress(count: Any, target: Any) -> float:
    numerator, denominator = _number(count), _number(target)
    if numerator is None or denominator is None or denominator <= 0:
        return 0.0
    return max(0.0, min(100.0, numerator / denominator * 100))


def render_html(model: dict[str, Any]) -> str:
    reversal = model["reversal"]
    collector = model["collector"]
    liveness = model["liveness"]
    pin = model["pin"]
    condor = model["iron_condor"]
    databento = model["databento"]
    reversal_progress = _progress(reversal["count"], reversal["target"])
    pin_progress = _progress(pin["available_days"], pin["target_days"])
    condor_progress = _progress(condor["settled_days"], condor["target_days"])
    counts = collector.get("counts") or {}
    count_items = "".join(
        f'<span><b>{_fmt_int(value)}</b>{_e(str(key).replace("_", " "))}</span>'
        for key, value in list(counts.items())[:4]
    ) or '<span><b>—</b>not yet</span>'
    collector_tone = "good" if collector.get("state") == "ACTIVE_RTH_CAPTURE" else "watch"
    liveness_tone = "good" if str(liveness.get("kind")).lower() == "ok" else "watch"
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scalpr Status Monitor</title>
<style>
:root{{--ink:#101c26;--navy:#132b3a;--paper:#f2efe5;--card:#fbfaf5;--line:#c8c7bc;--mint:#72c7a0;--amber:#dda448;--coral:#d96d55;--steel:#667883;--shadow:0 14px 42px rgba(16,28,38,.10)}}
*{{box-sizing:border-box}} body{{margin:0;background:radial-gradient(circle at 80% 0,#dce9df 0,transparent 32%),linear-gradient(135deg,#ede9dc,#f5f2e9 54%,#e7ede8);color:var(--ink);font-family:"Avenir Next Condensed","DIN Condensed","Franklin Gothic Condensed",sans-serif;min-height:100vh}}
body:before{{content:"";position:fixed;inset:0;pointer-events:none;opacity:.19;background-image:linear-gradient(rgba(19,43,58,.10) 1px,transparent 1px),linear-gradient(90deg,rgba(19,43,58,.08) 1px,transparent 1px);background-size:36px 36px}}
.shell{{position:relative;max-width:1240px;margin:auto;padding:34px 24px 48px}} header{{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:3px solid var(--ink);padding-bottom:18px;margin-bottom:18px}} .eyebrow,.badge,.label{{font-family:"SFMono-Regular",Menlo,monospace;text-transform:uppercase;letter-spacing:.13em}} .eyebrow{{font-size:11px;color:var(--steel)}} h1{{font-family:Georgia,"Times New Roman",serif;font-size:clamp(38px,7vw,76px);line-height:.82;margin:9px 0 0;letter-spacing:-.065em;font-weight:500}} .stamp{{font-family:"SFMono-Regular",Menlo,monospace;text-align:right;font-size:11px;line-height:1.7;color:var(--steel)}}
.authority{{display:flex;gap:8px;flex-wrap:wrap;margin:15px 0 26px}} .badge{{font-size:9px;padding:5px 8px;border:1px solid;border-radius:999px;font-weight:700}} .badge.inferential{{background:#e8f1ec;border-color:#77a58d;color:#285d45}} .badge.shadow{{background:#fff2dc;border-color:#d5a249;color:#79510e}} .badge.ops{{background:#e8eef1;border-color:#8da1ac;color:#344e5c}} .badge.lock{{background:var(--ink);border-color:var(--ink);color:white}}
.grid{{display:grid;grid-template-columns:1.16fr .84fr;gap:16px}} .card{{background:rgba(251,250,245,.92);border:1px solid var(--line);box-shadow:var(--shadow);padding:20px;min-width:0}} .card.hero{{grid-row:span 2}} .card.wide{{grid-column:1/-1}} .card-head{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:16px}} .kicker{{font-family:Georgia,serif;font-size:24px;letter-spacing:-.03em}} .sub{{font-size:11px;color:var(--steel);margin-top:3px}} .label{{font-size:9px;color:var(--steel)}}
.big{{font-family:Georgia,serif;font-size:58px;letter-spacing:-.06em;line-height:1}} .big small{{font:12px "SFMono-Regular",Menlo,monospace;letter-spacing:.04em;color:var(--steel)}} .status{{display:inline-flex;align-items:center;gap:8px;font-weight:700}} .dot{{width:9px;height:9px;border-radius:50%;background:var(--amber);box-shadow:0 0 0 4px rgba(221,164,72,.14)}} .status.good .dot{{background:var(--mint);box-shadow:0 0 0 4px rgba(114,199,160,.16)}}
.progress{{height:10px;background:#dfe1da;margin:14px 0 8px;overflow:hidden}} .progress i{{display:block;height:100%;background:linear-gradient(90deg,var(--navy),var(--mint));width:var(--p)}} .meta{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:18px}} .metric{{border-left:2px solid var(--line);padding-left:10px}} .metric b{{display:block;font:22px Georgia,serif;margin-top:4px}} .metric span{{font-size:11px;color:var(--steel)}}
.safety{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:15px 0}} .safety div{{padding:10px;background:#eef0ea}} .safety b{{display:block;font-size:12px;margin-top:4px}} .counts{{display:flex;gap:18px;flex-wrap:wrap;border-top:1px dashed var(--line);padding-top:13px}} .counts span{{font-size:10px;color:var(--steel)}} .counts b{{font:19px Georgia,serif;color:var(--ink);display:block}}
.reading{{font:32px Georgia,serif;letter-spacing:-.035em;margin:2px 0 12px;overflow-wrap:anywhere}} .pocket{{display:flex;align-items:center;gap:9px;font-family:"SFMono-Regular",Menlo,monospace;font-size:12px;background:#edf0ea;padding:10px}} .pocket i{{height:1px;background:var(--steel);flex:1}} .disclaimer{{font-size:10px;line-height:1.5;color:var(--steel);border-top:1px dashed var(--line);padding-top:10px;margin-top:14px;text-transform:uppercase;letter-spacing:.06em}}
.tail{{display:grid;grid-template-columns:repeat(5,1fr);gap:9px;margin-top:14px}} .tail div{{background:#eef0ea;padding:11px}} .tail b{{display:block;font:19px Georgia,serif;margin-top:4px}} footer{{display:flex;justify-content:space-between;gap:20px;border-top:3px solid var(--ink);margin-top:18px;padding-top:13px;font:10px/1.6 "SFMono-Regular",Menlo,monospace;color:var(--steel)}}
@media(max-width:820px){{.grid{{grid-template-columns:1fr}}.card.hero{{grid-row:auto}}.card.wide{{grid-column:auto}}.meta,.tail{{grid-template-columns:repeat(2,1fr)}}header{{align-items:flex-start}}.stamp{{max-width:42%}}}} @media(max-width:520px){{.shell{{padding:24px 14px 34px}}h1{{font-size:48px}}.safety{{grid-template-columns:1fr}}.meta{{grid-template-columns:1fr 1fr}}footer{{display:block}}}}
</style>
</head>
<body><main class="shell">
<header><div><div class="eyebrow">System evidence / morning brief</div><h1>SCALPR<br>STATUS</h1></div><div class="stamp">READ-ONLY RENDER<br>{_e(model['generated_at_utc'])}<br>NO TRADE AUTHORITY</div></header>
<div class="authority"><span class="badge lock">execution authority: false</span><span class="badge lock">admission authority: false</span><span class="badge ops">ledger inputs: read only</span></div>
<section class="grid">
<article class="card hero"><div class="card-head"><div><div class="kicker">Reversal accrual</div><div class="sub">Frozen A2 prospective evidence gate</div></div><span class="badge inferential">{INFERENTIAL_BADGE}</span></div>
<div class="big">{_fmt_int(reversal['count'])}<small> / {_fmt_int(reversal['target'])} CLEAN EPISODES</small></div><div class="progress" style="--p:{reversal_progress:.2f}%"><i></i></div><div class="label">{reversal_progress:.1f}% accrued · {_fmt_int(reversal['remaining'])} remaining</div>
<div class="meta"><div class="metric"><span>Phase</span><b>{_e(reversal['phase'])}</b></div><div class="metric"><span>Integrity</span><b>{_e(reversal['integrity'])}</b></div><div class="metric"><span>As of</span><b>{_e(reversal['as_of'])}</b></div></div>
<div class="disclaimer">Inferential track, still underpowered. Accrual status is not an edge verdict and never authorizes a trade.</div></article>
<article class="card"><div class="card-head"><div><div class="kicker">Collector + safety</div><div class="sub">Capture worker and authority boundary</div></div><span class="badge ops">{OPERATIONS_BADGE}</span></div>
<div class="status {collector_tone}"><span class="dot"></span>{_e(collector['state'])}</div><div class="label" style="margin-top:6px">{_e(collector['version'])} · heartbeat {_fmt_age(collector['heartbeat_age'])}</div>
<div class="safety"><div><span class="label">Execution</span><b>{_bool_state(collector['execution_authority'],true_text='ENABLED',false_text='DISABLED')}</b></div><div><span class="label">Guard access</span><b>{_bool_state(collector['guard_access'],true_text='YES',false_text='NO')}</b></div><div><span class="label">Cohorts</span><b>{_bool_state(collector['cohorts_locked'],true_text='LOCKED',false_text='UNLOCKED')}</b></div></div>
<div class="label">{_e(collector['count_label'])}</div><div class="counts">{count_items}</div></article>
<article class="card"><div class="card-head"><div><div class="kicker">Liveness alert</div><div class="sub">Latest watcher transition / exit</div></div><span class="badge ops">{OPERATIONS_BADGE}</span></div>
<div class="status {liveness_tone}"><span class="dot"></span>{_e(liveness['event'])} · {_e(liveness['kind'])}</div><div class="reading" style="font-size:20px;margin-top:12px">{_e(liveness['message'])}</div><div class="label">{_e(liveness['at'])} · {_e(liveness['source'])}</div></article>
<article class="card"><div class="card-head"><div><div class="kicker">SPY pin pressure</div><div class="sub">Latest FlashAlpha candidate reading</div></div><span class="badge shadow">{SHADOW_BADGE}</span></div>
<div class="reading">{_e(pin['grade'])}</div><div class="pocket"><span>{_fmt_price(pin['put_wall'])}</span><i></i><b>SPY {_fmt_price(pin['spot'])}</b><i></i><span>{_fmt_price(pin['call_wall'])}</span></div>
<div class="meta"><div class="metric"><span>Provider score</span><b>{_fmt_price(pin['pin_score'])}</b></div><div class="metric"><span>Gamma</span><b>{_e(pin['gamma_regime'])}</b></div><div class="metric"><span>Days scored</span><b>{_fmt_int(pin['available_days'])}/{_fmt_int(pin['target_days'])}</b></div></div><div class="progress" style="--p:{pin_progress:.2f}%"><i></i></div>
<div class="disclaimer">Candidate state, not confirmed; provider pressure score is not a calibrated probability and not a trade signal.</div></article>
<article class="card"><div class="card-head"><div><div class="kicker">0DTE iron condor</div><div class="sub">Defined-risk paper settlement study</div></div><span class="badge shadow">{SHADOW_BADGE}</span></div>
<div class="big">{_fmt_int(condor['settled_days'])}<small> / {_fmt_int(condor['target_days'])} SETTLED DAYS</small></div><div class="progress" style="--p:{condor_progress:.2f}%"><i></i></div>
<div class="meta"><div class="metric"><span>Mean P&amp;L</span><b>{_fmt_money(condor['mean_pnl'])}</b></div><div class="metric"><span>Win rate</span><b>{_fmt_pct(condor['win_rate'])}</b></div><div class="metric"><span>Net expectancy</span><b>{_fmt_money(condor['expectancy'])}</b></div></div>
<div class="tail"><div><span class="label">Worst day</span><b>{_fmt_money(condor['worst_day'])}</b></div><div><span class="label">P10</span><b>{_fmt_money(condor['p10'])}</b></div><div><span class="label">Bottom decile</span><b>{_fmt_money(condor['bottom_decile'])}</b></div><div><span class="label">Median</span><b>{_fmt_money(condor['median_pnl'])}</b></div><div><span class="label">Settled</span><b>{_fmt_int(condor['settled_days'])}</b></div></div>
<div class="disclaimer">Shadow paper P&amp;L only. Failed-pin tail and costs are included; no edge or execution verdict.</div></article>
<article class="card wide"><div class="card-head"><div><div class="kicker">Databento historical probe</div><div class="sub">Sparse-window exploratory coverage and spend gate</div></div><span class="badge shadow">{SHADOW_BADGE}</span></div>
<div class="meta"><div class="metric"><span>Detected episodes</span><b>{_fmt_int(databento['episode_count'])}</b></div><div class="metric"><span>Quote windows</span><b>{_fmt_int(databento['window_count'])}</b></div><div class="metric"><span>Cost probe</span><b>{_e(databento['probe_status'])}</b></div></div>
<div class="tail"><div><span class="label">Sample windows</span><b>{_fmt_int(databento['sample_windows'])}</b></div><div><span class="label">Probe charged</span><b>{_fmt_money(databento['actual_probe_cost'])}</b></div><div><span class="label">Projected quotes</span><b>{_fmt_int(databento['projected_quote_records'])}</b></div><div><span class="label">Projected cost</span><b>{_fmt_money(databento['projected_quote_cost'])}</b></div><div><span class="label">Authority</span><b>NONE</b></div></div>
<div class="disclaimer">Historical smell test and pipeline validation only; excluded from the inferential 200.</div></article>
</section>
<footer><span>Generated by {MODULE_VERSION}<br>Missing inputs remain — / not yet.</span><span>READ ONLY · NO ORDERS · NO ADMISSION<br>Shadow studies never render as verdicts.</span></footer>
</main></body></html>'''


def write_html_atomic(path: Path, content: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def render(
    *,
    root: Path,
    output: Path,
    pin_root: Path | None = None,
    databento_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    sources = discover_sources(root, pin_root=pin_root, databento_root=databento_root)
    model = gather_status(sources, now=now or datetime.now(timezone.utc))
    write_html_atomic(output, render_html(model))
    return {
        "output": str(output.resolve()),
        "generated_at_utc": model["generated_at_utc"],
        "read_only_sources": True,
        "execution_authority": False,
        "admission_authority": False,
        "order_authority": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the read-only Scalpr status monitor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    render_parser = subparsers.add_parser("render", help="regenerate the self-contained HTML page")
    render_parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    render_parser.add_argument("--pin-root", type=Path)
    render_parser.add_argument("--databento-root", type=Path)
    render_parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT))
    args = parser.parse_args()
    result = render(
        root=args.root, output=args.output, pin_root=args.pin_root,
        databento_root=args.databento_root,
    )
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
