"""Bounded, read-only dense-source tooling for frozen A2 measurements.

This module is intentionally standalone.  It is not imported by the server and
does not construct a trading client.  Authenticated pulls are operator-run from
Terminal after loading the existing Keychain-backed environment.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

import requests

import a2_accrual_store as accrual_store
import a2_measurement as a2


SOURCE_VERSION = a2.DENSE_ENDPOINT_SOURCE
BASE_URL = "https://data.alpaca.markets"
QUOTES_PATH = "/v2/stocks/quotes"
TRADES_PATH = "/v2/stocks/trades"
DEFAULT_DENSE_LABELS_PATH = accrual_store.DENSE_LABELS_PATH
DEFAULT_DENSE_SUMMARY_PATH = accrual_store.DENSE_SUMMARY_PATH
DEFAULT_COMPARISON_PATH = accrual_store.DENSE_COMPARISON_PATH
DEFAULT_BIAS_PATH = a2.DEFAULT_OUTPUT_DIR / "a2_capture_gap_bias_v0.json"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_FETCH_DEADLINE_SECONDS = 300.0
DEFAULT_MAX_PAGES = 500
BIAS_ALPHA = 0.05
BIAS_MIN_ABS_RANK_BISERIAL = 0.10


class DenseSourceError(RuntimeError):
    """A bounded read-only provider operation could not be completed."""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_quote(row: dict[str, Any]) -> dict[str, Any] | None:
    provider_ts = a2._parse_dt(row.get("t") or row.get("timestamp") or row.get("provider_ts"))
    try:
        bid = float(row.get("bp") if row.get("bp") is not None else row.get("bid"))
        ask = float(row.get("ap") if row.get("ap") is not None else row.get("ask"))
    except (TypeError, ValueError):
        return None
    if provider_ts is None or bid <= 0 or ask <= 0 or bid > ask:
        return None
    return {
        "provider_ts": provider_ts,
        "received_at": None,
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2.0,
        "source": SOURCE_VERSION,
        "endpoint_source": SOURCE_VERSION,
    }


def _source_trade(row: dict[str, Any]) -> dict[str, Any] | None:
    provider_ts = a2._parse_dt(row.get("t") or row.get("timestamp") or row.get("provider_ts"))
    try:
        price = float(row.get("p") if row.get("p") is not None else row.get("price"))
    except (TypeError, ValueError):
        return None
    if provider_ts is None or price <= 0:
        return None
    return {"provider_ts": provider_ts, "price": price}


class AlpacaHistoricalStockDataClient:
    """Read-only SIP quote/trade client with per-request and total deadlines."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        session: Any | None = None,
        base_url: str = BASE_URL,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        fetch_deadline_seconds: float = DEFAULT_FETCH_DEADLINE_SECONDS,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        if not api_key or not secret_key:
            raise DenseSourceError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        self.base_url = base_url.rstrip("/")
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.fetch_deadline_seconds = float(fetch_deadline_seconds)
        self.max_pages = int(max_pages)
        if (
            self.request_timeout_seconds <= 0
            or self.fetch_deadline_seconds <= 0
            or self.max_pages <= 0
        ):
            raise DenseSourceError("request timeout, fetch deadline, and max pages must be positive")
        self.session = session or requests.Session()
        self.session.headers.update({
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
            "User-Agent": "Scalpr-A2-Dense-Source/1.0",
        })

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "AlpacaHistoricalStockDataClient":
        return cls(
            (os.getenv("ALPACA_API_KEY") or "").strip(),
            (os.getenv("ALPACA_SECRET_KEY") or "").strip(),
            **kwargs,
        )

    def _records(
        self, path: str, payload_key: str, *, symbol: str,
        start: datetime, end: datetime,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + self.fetch_deadline_seconds
        params: dict[str, Any] = {
            "symbols": symbol.upper(),
            "start": _iso(start),
            "end": _iso(end),
            "feed": "sip",
            "sort": "asc",
            "limit": 10000,
        }
        records: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(self.max_pages):
            if time.monotonic() >= deadline:
                raise DenseSourceError(
                    f"Alpaca {payload_key} fetch exceeded {self.fetch_deadline_seconds:.0f}s deadline")
            if token:
                params["page_token"] = token
            else:
                params.pop("page_token", None)
            try:
                response = self.session.get(
                    self.base_url + path,
                    params=params,
                    timeout=self.request_timeout_seconds,
                )
            except requests.RequestException as exc:
                raise DenseSourceError(f"Alpaca {payload_key} request failed: {exc}") from exc
            if response.status_code != 200:
                try:
                    detail = response.json().get("message") or response.text
                except Exception:
                    detail = response.text
                detail = str(detail or "request rejected").replace("\n", " ")[:180]
                raise DenseSourceError(
                    f"Alpaca {payload_key} API HTTP {response.status_code}: {detail}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise DenseSourceError(f"Alpaca {payload_key} API returned invalid JSON") from exc
            records.extend((payload.get(payload_key) or {}).get(symbol.upper(), []))
            token = payload.get("next_page_token")
            if not token:
                return records
        raise DenseSourceError(
            f"Alpaca {payload_key} pagination exceeded {self.max_pages} pages")

    def historical_quotes(
        self, symbol: str, *, start: datetime, end: datetime,
    ) -> list[dict[str, Any]]:
        rows = self._records(QUOTES_PATH, "quotes", symbol=symbol, start=start, end=end)
        quotes = [quote for row in rows if (quote := _source_quote(row)) is not None]
        return sorted(quotes, key=lambda row: row["provider_ts"])

    def historical_trades(
        self, symbol: str, *, start: datetime, end: datetime,
    ) -> list[dict[str, Any]]:
        rows = self._records(TRADES_PATH, "trades", symbol=symbol, start=start, end=end)
        trades = [trade for row in rows if (trade := _source_trade(row)) is not None]
        return sorted(trades, key=lambda row: row["provider_ts"])


def _merge_windows(windows: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for start, end in sorted(windows):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def endpoint_fetch_windows(
    episodes: Iterable[dict[str, Any]],
) -> dict[str, list[tuple[datetime, datetime]]]:
    """Return only the fixed anchor/horizon windows required by the frozen basis."""
    by_session: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for episode in episodes:
        decided_at = a2._parse_dt(episode.get("decided_at"))
        session_date = str(episode.get("session_date") or "")
        if decided_at is None or not session_date:
            continue
        for minutes in (0, *a2.HORIZONS_MIN):
            boundary = decided_at + timedelta(minutes=minutes)
            by_session[session_date].append((
                boundary - timedelta(seconds=a2.MAX_POINT_OFFSET_SECONDS),
                boundary + timedelta(microseconds=1),
            ))
    return {session: _merge_windows(windows) for session, windows in by_session.items()}


def fetch_dense_endpoint_sessions(
    client: AlpacaHistoricalStockDataClient,
    episodes: Iterable[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    sessions: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []
    windows_by_session = endpoint_fetch_windows(episodes)
    for session_date, windows in windows_by_session.items():
        unique: dict[tuple[str, float, float], dict[str, Any]] = {}
        for start, end in windows:
            try:
                quotes = client.historical_quotes("SPY", start=start, end=end)
            except DenseSourceError as exc:
                errors.append({
                    "session_date": session_date,
                    "start": _iso(start),
                    "end": _iso(end),
                    "error": str(exc),
                })
                continue
            for quote in quotes:
                key = (quote["provider_ts"].isoformat(), quote["bid"], quote["ask"])
                unique[key] = quote
        sessions[session_date] = sorted(unique.values(), key=lambda row: row["provider_ts"])
    return sessions, {
        "endpoint_source": SOURCE_VERSION,
        "sessions_requested": sorted(windows_by_session),
        "windows_requested": sum(len(windows) for windows in windows_by_session.values()),
        "clean_quotes_fetched": sum(len(rows) for rows in sessions.values()),
        "fetch_errors": errors,
        "fetch_status": "PASS" if not errors else "PARTIAL_UNAVAILABLE",
    }


def _point_proof(label: dict[str, Any]) -> dict[str, Any]:
    decided_at = a2._parse_dt(label.get("decided_at"))
    points: dict[str, Any] = {}
    if decided_at is None:
        return points
    points["anchor"] = {
        "boundary": decided_at.isoformat(),
        "provider_ts": label.get("anchor_provider_ts"),
        "age_seconds": label.get("anchor_age_seconds"),
        "endpoint_source": label.get("anchor_source"),
    }
    for horizon in a2.HORIZONS_MIN:
        key = f"{horizon}m"
        points[key] = {
            "boundary": (decided_at + timedelta(minutes=horizon)).isoformat(),
            "provider_ts": (label.get("endpoint_provider_ts") or {}).get(key),
            "age_seconds": (label.get("endpoint_age_seconds") or {}).get(key),
            "endpoint_source": (label.get("endpoint_sources") or {}).get(key),
        }
    return points


def _valid_dense_point(point: dict[str, Any]) -> bool:
    boundary = a2._parse_dt(point.get("boundary"))
    provider_ts = a2._parse_dt(point.get("provider_ts"))
    age = point.get("age_seconds")
    if boundary is None or provider_ts is None or age is None:
        return False
    return (
        point.get("endpoint_source") == SOURCE_VERSION
        and provider_ts <= boundary
        and 0.0 <= float(age) <= a2.MAX_POINT_OFFSET_SECONDS
    )


def build_source_comparison(
    legacy_labels: Iterable[dict[str, Any]],
    dense_labels: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    legacy = {str(row.get("episode_key")): row for row in legacy_labels}
    dense = {str(row.get("episode_key")): row for row in dense_labels}
    flips: list[dict[str, Any]] = []
    for episode_key in sorted(set(legacy) & set(dense)):
        old, new = legacy[episode_key], dense[episode_key]
        if old.get("label_status") != "AVAILABLE" and new.get("label_status") == "AVAILABLE":
            proof = _point_proof(new)
            flips.append({
                "episode_key": episode_key,
                "legacy_missing_reason": old.get("missing_reason"),
                "dense_label_record_id": new.get("label_record_id"),
                "point_in_time_proof": proof,
                "all_points_genuine_fresh_dense_quotes": bool(proof) and all(
                    _valid_dense_point(point) for point in proof.values()),
            })
    legacy_available = sum(row.get("label_status") == "AVAILABLE" for row in legacy.values())
    dense_available = sum(row.get("label_status") == "AVAILABLE" for row in dense.values())
    return {
        "schema_version": "a2-dense-source-comparison-v0",
        "legacy_endpoint_source": a2.LEGACY_ENDPOINT_SOURCE,
        "dense_endpoint_source": SOURCE_VERSION,
        "freshness_seconds": a2.MAX_POINT_OFFSET_SECONDS,
        "horizons_minutes": list(a2.HORIZONS_MIN),
        "all_horizons_required": True,
        "legacy_label_count": len(legacy),
        "dense_label_count": len(dense),
        "legacy_available_count": legacy_available,
        "dense_available_count": dense_available,
        "availability_rise_count": dense_available - legacy_available,
        "unavailable_to_available_flips": flips,
        "all_flips_have_genuine_fresh_dense_quotes": all(
            flip["all_points_genuine_fresh_dense_quotes"] for flip in flips),
        "dense_label_record_ids": sorted(
            str(row.get("label_record_id")) for row in dense.values()
            if row.get("label_record_id")
        ),
    }


def run_uniform_relabel(
    client: AlpacaHistoricalStockDataClient,
    *,
    episodes_path: Path = a2.DEFAULT_EPISODES,
    tick_log_path: Path = a2.DEFAULT_TICK_LOG,
    quarantine_path: Path | None = None,
    output_path: Path = DEFAULT_DENSE_LABELS_PATH,
    summary_path: Path = DEFAULT_DENSE_SUMMARY_PATH,
    comparison_path: Path = DEFAULT_COMPARISON_PATH,
) -> dict[str, Any]:
    quarantine_path = quarantine_path or episodes_path.with_name(a2.DEFAULT_QUARANTINE_MANIFEST.name)
    admitted = a2.load_episodes(
        episodes_path, admitted_only=True,
        quarantine_path=quarantine_path, exclude_quarantined=True)
    clean, _duplicates, _collisions, _collision_rows = a2.clean_a2_episodes(admitted)
    dense_sessions, fetch_report = fetch_dense_endpoint_sessions(client, clean)
    legacy_labels, legacy_summary = a2.measure_a2(
        episodes_path=episodes_path,
        tick_log_path=tick_log_path,
        quarantine_path=quarantine_path,
        endpoint_source=a2.LEGACY_ENDPOINT_SOURCE,
    )
    dense_labels, dense_summary = a2.measure_a2(
        episodes_path=episodes_path,
        tick_log_path=tick_log_path,
        quarantine_path=quarantine_path,
        quote_sessions=dense_sessions,
        endpoint_source=SOURCE_VERSION,
        path_quotes_complete=False,
    )
    dense_summary["records_appended"] = a2.append_jsonl(dense_labels, output_path)
    dense_summary["dense_fetch"] = fetch_report
    comparison = build_source_comparison(legacy_labels, dense_labels)
    dense_summary["source_comparison"] = comparison
    dense_summary["legacy_cross_check"] = {
        "endpoint_source": legacy_summary.get("endpoint_source"),
        "clean_a2_labelable_episode_count": legacy_summary.get("clean_a2_labelable_episode_count"),
        "clean_a2_unavailable_episode_count": legacy_summary.get("clean_a2_unavailable_episode_count"),
    }
    a2.write_summary(dense_summary, summary_path)
    a2.write_summary(comparison, comparison_path)
    return dense_summary


def load_tick_timestamps(
    path: Path = a2.DEFAULT_TICK_LOG, symbol: str = "SPY",
) -> dict[str, list[datetime]]:
    sessions: dict[str, list[datetime]] = defaultdict(list)
    if not path.exists():
        return {}
    with path.open(newline="") as source:
        for row in csv.DictReader(source):
            if str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            provider_ts = a2._parse_dt(row.get("provider_ts"))
            if provider_ts is None:
                continue
            session_date = provider_ts.astimezone(a2.ET).date().isoformat()
            open_at, close_at = a2._session_bounds(session_date)
            if open_at <= provider_ts < close_at:
                sessions[session_date].append(provider_ts)
    return {key: sorted(set(values)) for key, values in sessions.items()}


def identify_capture_gaps(
    tick_sessions: dict[str, list[datetime]],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for session_date, timestamps in sorted(tick_sessions.items()):
        open_at, _close_at = a2._session_bounds(session_date)
        for previous, current in zip(timestamps, timestamps[1:]):
            duration = (current - previous).total_seconds()
            if duration <= a2.MAX_POINT_OFFSET_SECONDS:
                continue
            first_minute = max(0, int((previous - open_at).total_seconds() // 60))
            last_minute = min(389, int((current - open_at).total_seconds() // 60))
            gaps.append({
                "session_date": session_date,
                "start": previous.isoformat(),
                "end": current.isoformat(),
                "duration_seconds": round(duration, 6),
                "session_minutes": list(range(first_minute, last_minute + 1)),
            })
    return gaps


def _minute_index(provider_ts: datetime, session_date: str) -> int:
    open_at, _close_at = a2._session_bounds(session_date)
    return int((provider_ts - open_at).total_seconds() // 60)


def build_activity_minutes(
    dense_quotes: dict[str, list[dict[str, Any]]],
    dense_trades: dict[str, list[dict[str, Any]]],
    gaps: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    gap_minutes = {
        (gap["session_date"], minute)
        for gap in gaps for minute in gap["session_minutes"]
    }
    rows: list[dict[str, Any]] = []
    for session_date, quotes in sorted(dense_quotes.items()):
        quote_buckets: dict[int, list[float]] = defaultdict(list)
        trade_counts: dict[int, int] = defaultdict(int)
        for quote in quotes:
            minute = _minute_index(quote["provider_ts"], session_date)
            if 0 <= minute < 390:
                quote_buckets[minute].append(float(quote["mid"]))
        for trade in dense_trades.get(session_date, []):
            minute = _minute_index(trade["provider_ts"], session_date)
            if 0 <= minute < 390:
                trade_counts[minute] += 1

        five_minute_bars: dict[int, dict[str, float]] = {}
        for minute, mids in quote_buckets.items():
            bucket = minute // 5
            bar = five_minute_bars.setdefault(bucket, {
                "high": mids[0], "low": mids[0], "close": mids[-1]})
            bar["high"] = max(bar["high"], max(mids))
            bar["low"] = min(bar["low"], min(mids))
            bar["close"] = mids[-1]
        atr_by_bucket: dict[int, float] = {}
        true_ranges: list[tuple[int, float]] = []
        previous_close: float | None = None
        for bucket, bar in sorted(five_minute_bars.items()):
            true_range = bar["high"] - bar["low"]
            if previous_close is not None:
                true_range = max(
                    true_range,
                    abs(bar["high"] - previous_close),
                    abs(bar["low"] - previous_close),
                )
            true_ranges.append((bucket, true_range))
            if len(true_ranges) >= 14:
                atr_by_bucket[bucket] = mean(value for _key, value in true_ranges[-14:])
            previous_close = bar["close"]

        for minute, mids in sorted(quote_buckets.items()):
            if not mids:
                continue
            rows.append({
                "session_date": session_date,
                "session_minute": minute,
                "is_gap_minute": (session_date, minute) in gap_minutes,
                "abs_return_1m": abs(math.log(mids[-1] / mids[0])) if mids[0] > 0 else None,
                "quote_trade_intensity": len(mids) + trade_counts.get(minute, 0),
                "atr_5m": atr_by_bucket.get(minute // 5),
            })
    return rows


def _ranked(values: list[float]) -> tuple[list[float], list[int]]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    tie_sizes: list[int] = []
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        average_rank = ((cursor + 1) + end) / 2.0
        for original_index, _value in indexed[cursor:end]:
            ranks[original_index] = average_rank
        tie_sizes.append(end - cursor)
        cursor = end
    return ranks, tie_sizes


def mann_whitney_characterization(gap: list[float], clean: list[float]) -> dict[str, Any]:
    if not gap or not clean:
        return {"status": "UNAVAILABLE", "gap_n": len(gap), "clean_n": len(clean)}
    values = [*gap, *clean]
    ranks, ties = _ranked(values)
    n_gap, n_clean = len(gap), len(clean)
    u_gap = sum(ranks[:n_gap]) - n_gap * (n_gap + 1) / 2.0
    expected = n_gap * n_clean / 2.0
    n_total = n_gap + n_clean
    tie_term = sum(size ** 3 - size for size in ties)
    variance = n_gap * n_clean / 12.0 * (
        n_total + 1 - tie_term / (n_total * (n_total - 1))
    ) if n_total > 1 else 0.0
    if variance <= 0:
        p_value = 1.0 if u_gap == expected else 0.0
    else:
        difference = abs(u_gap - expected)
        z = max(0.0, difference - 0.5) / math.sqrt(variance)
        p_value = math.erfc(z / math.sqrt(2.0))
    effect = (2.0 * u_gap / (n_gap * n_clean)) - 1.0
    return {
        "status": "MEASURED",
        "gap_n": n_gap,
        "clean_n": n_clean,
        "gap_mean": mean(gap),
        "clean_mean": mean(clean),
        "mann_whitney_u_gap": u_gap,
        "two_sided_p_value": p_value,
        "rank_biserial_effect": effect,
    }


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def characterize_gap_bias(activity_rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(activity_rows)
    metrics = ("abs_return_1m", "quote_trade_intensity", "atr_5m")
    tests: dict[str, Any] = {}
    top_quartile: dict[str, Any] = {}
    correlated: list[str] = []
    for metric in metrics:
        gap = [float(row[metric]) for row in rows if row.get("is_gap_minute") and row.get(metric) is not None]
        clean = [float(row[metric]) for row in rows if not row.get("is_gap_minute") and row.get(metric) is not None]
        result = mann_whitney_characterization(gap, clean)
        tests[metric] = result
        threshold = _quantile([*gap, *clean], 0.75)
        top_quartile[metric] = {
            "threshold": threshold,
            "gap_minute_fraction": (
                sum(value >= threshold for value in gap) / len(gap)
                if gap and threshold is not None else None
            ),
        }
        if (
            result.get("status") == "MEASURED"
            and float(result["two_sided_p_value"]) <= BIAS_ALPHA
            and abs(float(result["rank_biserial_effect"])) >= BIAS_MIN_ABS_RANK_BISERIAL
        ):
            correlated.append(metric)
    measured = [result for result in tests.values() if result.get("status") == "MEASURED"]
    verdict = (
        "UNAVAILABLE" if not measured else
        "GAPS_CONDITION_CORRELATED" if correlated else "GAPS_RANDOM"
    )
    return {
        "schema_version": "a2-capture-gap-bias-v0",
        "verdict": verdict,
        "characterization_only": True,
        "cohort_action": False,
        "gap_definition_seconds": a2.MAX_POINT_OFFSET_SECONDS,
        "alpha": BIAS_ALPHA,
        "minimum_absolute_rank_biserial_effect": BIAS_MIN_ABS_RANK_BISERIAL,
        "correlated_metrics": correlated,
        "activity_tests": tests,
        "top_activity_quartile": top_quartile,
        "gap_minute_count": sum(bool(row.get("is_gap_minute")) for row in rows),
        "clean_minute_count": sum(not bool(row.get("is_gap_minute")) for row in rows),
    }


def run_bias_check(
    client: AlpacaHistoricalStockDataClient,
    *, tick_log_path: Path = a2.DEFAULT_TICK_LOG,
    recent_sessions: int = 10,
    output_path: Path = DEFAULT_BIAS_PATH,
) -> dict[str, Any]:
    tick_sessions = load_tick_timestamps(tick_log_path)
    selected_dates = sorted(tick_sessions)[-max(1, int(recent_sessions)):]
    tick_sessions = {date: tick_sessions[date] for date in selected_dates}
    dense_quotes: dict[str, list[dict[str, Any]]] = {}
    dense_trades: dict[str, list[dict[str, Any]]] = {}
    provider_errors: list[dict[str, str]] = []
    for session_date in selected_dates:
        open_at, close_at = a2._session_bounds(session_date)
        try:
            quotes = client.historical_quotes(
                "SPY", start=open_at, end=close_at)
            trades = client.historical_trades(
                "SPY", start=open_at, end=close_at)
        except DenseSourceError as exc:
            provider_errors.append({"session_date": session_date, "error": str(exc)})
        else:
            dense_quotes[session_date] = quotes
            dense_trades[session_date] = trades
    gaps = identify_capture_gaps(tick_sessions)
    activity_rows = build_activity_minutes(dense_quotes, dense_trades, gaps)
    report = characterize_gap_bias(activity_rows)
    activity_by_minute = {
        (row["session_date"], row["session_minute"]): row for row in activity_rows
    }
    gap_event_top_quartile: dict[str, Any] = {}
    for metric, quartile in report["top_activity_quartile"].items():
        threshold = quartile["threshold"]
        values = []
        if threshold is not None:
            for gap in gaps:
                minute = gap["session_minutes"][0]
                row = activity_by_minute.get((gap["session_date"], minute))
                if row is not None and row.get(metric) is not None:
                    values.append(float(row[metric]))
        gap_event_top_quartile[metric] = {
            "eligible_gap_count": len(values),
            "fraction": (
                sum(value >= threshold for value in values) / len(values)
                if values and threshold is not None else None
            ),
        }
    report.update({
        "endpoint_source": SOURCE_VERSION,
        "sessions_requested": selected_dates,
        "sessions_measured": sorted(dense_quotes),
        "interarrival_gap_count": len(gaps),
        "gap_event_top_activity_quartile": gap_event_top_quartile,
        "provider_errors": provider_errors,
        "provider_status": "PASS" if not provider_errors else "PARTIAL_UNAVAILABLE",
    })
    a2.write_summary(report, output_path)
    return report


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open() as source:
        return [json.loads(line) for line in source if line.strip()]


def verify_uniform_relabel(
    *, labels_path: Path = DEFAULT_DENSE_LABELS_PATH,
    comparison_path: Path = DEFAULT_COMPARISON_PATH,
) -> dict[str, Any]:
    comparison = json.loads(comparison_path.read_text())
    wanted = set(comparison.get("dense_label_record_ids") or [])
    dense_labels = [
        row for row in _read_jsonl(labels_path)
        if row.get("label_record_id") in wanted
    ]
    provenance_ok = bool(dense_labels) and all(
        row.get("endpoint_source") == SOURCE_VERSION
        and row.get("horizons_minutes") == list(a2.HORIZONS_MIN)
        and row.get("max_point_offset_seconds") == a2.MAX_POINT_OFFSET_SECONDS
        and all(source == SOURCE_VERSION for source in (row.get("endpoint_sources") or {}).values())
        for row in dense_labels
    )
    dense_available = int(comparison.get("dense_available_count") or 0)
    legacy_available = int(comparison.get("legacy_available_count") or 0)
    flips = comparison.get("unavailable_to_available_flips") or []
    checks = {
        "all_dense_run_labels_present": len(dense_labels) == len(wanted),
        "provenance_and_frozen_rules_intact": provenance_ok,
        "availability_rises_vs_tick_log": dense_available > legacy_available,
        "every_flip_has_genuine_fresh_dense_quotes": bool(flips) and all(
            flip.get("all_points_genuine_fresh_dense_quotes") for flip in flips),
    }
    return {
        "schema_version": "a2-dense-source-verification-v0",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "dense_labels_verified": len(dense_labels),
        "legacy_available_count": legacy_available,
        "dense_available_count": dense_available,
        "availability_rise_count": dense_available - legacy_available,
        "flip_count": len(flips),
    }


def _client_from_args(args: argparse.Namespace) -> AlpacaHistoricalStockDataClient:
    return AlpacaHistoricalStockDataClient.from_environment(
        request_timeout_seconds=args.request_timeout_seconds,
        fetch_deadline_seconds=args.fetch_deadline_seconds,
        max_pages=args.max_pages,
    )


def _add_provider_limits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--request-timeout-seconds", type=float, default=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    parser.add_argument("--fetch-deadline-seconds", type=float, default=DEFAULT_FETCH_DEADLINE_SECONDS)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    relabel = commands.add_parser("relabel", help="uniformly re-label clean accrued episodes")
    relabel.add_argument("--episodes", type=Path, default=a2.DEFAULT_EPISODES)
    relabel.add_argument("--tick-log", type=Path, default=a2.DEFAULT_TICK_LOG)
    relabel.add_argument("--quarantine", type=Path, default=a2.DEFAULT_QUARANTINE_MANIFEST)
    relabel.add_argument("--labels", type=Path, default=DEFAULT_DENSE_LABELS_PATH)
    relabel.add_argument("--summary", type=Path, default=DEFAULT_DENSE_SUMMARY_PATH)
    relabel.add_argument("--comparison", type=Path, default=DEFAULT_COMPARISON_PATH)
    _add_provider_limits(relabel)

    bias = commands.add_parser("bias-check", help="characterize live tick capture-gap bias")
    bias.add_argument("--tick-log", type=Path, default=a2.DEFAULT_TICK_LOG)
    bias.add_argument("--recent-sessions", type=int, default=10)
    bias.add_argument("--output", type=Path, default=DEFAULT_BIAS_PATH)
    _add_provider_limits(bias)

    verify = commands.add_parser("verify", help="verify dense provenance and genuine freshness flips")
    verify.add_argument("--labels", type=Path, default=DEFAULT_DENSE_LABELS_PATH)
    verify.add_argument("--comparison", type=Path, default=DEFAULT_COMPARISON_PATH)

    args = parser.parse_args(argv)
    try:
        if args.command == "relabel":
            report = run_uniform_relabel(
                _client_from_args(args),
                episodes_path=args.episodes,
                tick_log_path=args.tick_log,
                quarantine_path=args.quarantine,
                output_path=args.labels,
                summary_path=args.summary,
                comparison_path=args.comparison,
            )
        elif args.command == "bias-check":
            report = run_bias_check(
                _client_from_args(args),
                tick_log_path=args.tick_log,
                recent_sessions=args.recent_sessions,
                output_path=args.output,
            )
        else:
            report = verify_uniform_relabel(
                labels_path=args.labels,
                comparison_path=args.comparison,
            )
    except (DenseSourceError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "UNAVAILABLE", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
