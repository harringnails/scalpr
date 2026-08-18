"""Default-off, read-only Entry Intelligence executable-bid collector.

This worker owns no broker client and has no order, position, liquidation, or
Guard imports.  It reads market data, writes append-only shadow evidence, and
keeps every pre-lock record formally ineligible for cohort claims.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any

import entry_bid_capture_v1 as bid_capture
import entry_episode_research_v1 as episode_research
import entry_intelligence_v1 as intelligence
import decision_replay_v1 as replay
import entry_contract_data_v1 as contract_data
import entry_episode_integrity_v1 as episode_integrity
import feature_engine as fe
import regime_layer_v0
import scalpr_config


COLLECTOR_VERSION = "entry-bid-collector-v1.2"
ROOT = Path(__file__).resolve().parent
LOW_COHORT = ROOT / "frozen_cohort_low_reversal_v1.json"
HIGH_COHORT = ROOT / "frozen_cohort_high_reversal_v1.json"


def disabled_status() -> dict:
    """In-memory status only: the default-off path writes no evidence files."""
    return {
        "schema_version": "entry-bid-collector-status-v1",
        "collector_version": COLLECTOR_VERSION,
        "enabled": False,
        "state": "DISABLED_DEFAULT_OFF",
        "market_window": False,
        "execution_authority": False,
        "guard_access": False,
        "formal_cohort_eligible": False,
    }


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _equal_number(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _validate_draft_against_config(document: dict, config: dict, side: str) -> None:
    """Fail closed if the approved runtime thresholds drift from the draft."""
    if document.get("document_status") != "DRAFT_NOT_LOCKED":
        raise ValueError("collector accepts only an explicitly unlocked draft")
    if document.get("operator_confirmed") is not False:
        raise ValueError("collector must not activate a cohort lock")
    proposed = document["proposed"]
    if proposed.get("side") != side or proposed.get("mode") != "PAPER_SHADOW_ONLY":
        raise ValueError("draft side or mode mismatch")
    shared = config["direction"]["shared"]
    directional = config["direction"][side]
    rules = proposed["direction_rules"]
    for key in (
            "atr_method", "atr_period", "rsi_method", "rsi_period",
            "extension_lookback_bars", "extension_atr_multiple",
            "level_proximity_pct", "round_number_increment"):
        if not _equal_number(rules.get(key), shared.get(key)):
            raise ValueError(f"direction threshold drift: {side}.{key}")
    if not _equal_number(rules.get("rsi_threshold"), directional.get("rsi_threshold")):
        raise ValueError(f"direction threshold drift: {side}.rsi_threshold")
    execution = proposed["execution"]
    for key in (
            "quote_source", "max_quote_age_seconds", "max_spread_pct",
            "min_contract_volume", "min_open_interest", "delta_target",
            "delta_min", "delta_max", "max_ask_price"):
        if not _equal_number(execution.get(key), config["execution"].get(key)):
            raise ValueError(f"execution threshold drift: {key}")
    scope = proposed["scope"]
    for key in ("dte_min", "dte_max"):
        if not _equal_number(scope.get(key), config["execution"].get(key)):
            raise ValueError(f"scope threshold drift: {key}")
    outcome = proposed["outcome"]
    for key in (
            "label_version", "option_risk_fraction", "reward_risk",
            "bid_poll_interval_seconds", "max_fresh_gap_seconds",
            "minimum_coverage_fraction", "return_horizons_minutes"):
        if not _equal_number(outcome.get(key), config["outcome"].get(key)):
            raise ValueError(f"outcome threshold drift: {key}")
    if outcome.get("cost_model") != config["outcome"].get("cost_model"):
        raise ValueError("transaction cost model drift")
    bid_capture.validate_cost_model(outcome.get("cost_model") or {})


def _append_unique(record: dict, path: Path, identity_key: str) -> bool:
    identity = record.get(identity_key)
    if not identity:
        raise ValueError(f"{identity_key} required")
    if any(row.get(identity_key) == identity for row in fe._iter_jsonl(path)):
        return False
    return fe._atomic_append(path, record)


class EntryBidCollector:
    def __init__(self, *, stock_data, option_data, feed: str):
        self.config = scalpr_config.load_entry_intelligence_config()
        if not scalpr_config.entry_bid_capture_enabled():
            raise RuntimeError("collector feature flag is off")
        self.stock_data = stock_data
        self.option_data = option_data
        self.feed = str(feed).lower()
        self.cohorts = {
            "CALL": _load_json(LOW_COHORT),
            "PUT": _load_json(HIGH_COHORT),
        }
        for side, document in self.cohorts.items():
            _validate_draft_against_config(document, self.config, side)
        collector = self.config["collector"]
        self.status_path = ROOT / collector["status_log"]
        self.decision_path = ROOT / collector["decision_log"]
        self.episode_path = ROOT / collector["episode_log"]
        self.episode_quarantine_path = ROOT / episode_integrity.DEFAULT_QUARANTINE_MANIFEST
        self.integrity_event_path = ROOT / episode_integrity.DEFAULT_INTEGRITY_EVENT_LOG
        self.bid_path = ROOT / collector["bid_log"]
        self.outcome_path = ROOT / collector["outcome_log"]
        self.gate_result_path = ROOT / collector["gate_result_log"]
        self.evidence_source_path = ROOT / collector["evidence_source_log"]
        self.evidence_manifest_path = ROOT / collector["evidence_manifest_log"]
        self.decision_event_path = ROOT / collector["decision_event_log"]
        self.no_trade_plan_path = ROOT / collector["no_trade_plan_log"]
        self.no_trade_bid_path = ROOT / collector["no_trade_bid_log"]
        self.no_trade_outcome_path = ROOT / collector["no_trade_outcome_log"]
        self.last_decision_minute = None
        self.last_status_write = 0.0
        self.universe_refresh_attempt_date = None
        self.universe_refresh_attempt_at = None
        self.universe_refresh_error = None
        self.regime_prior_session_date = None
        self.regime_prior_session_bars: list[list[dict]] = []
        self.regime_prior_session_error = None
        self.latest_regime_inputs: dict | None = None
        self.last_universe_as_of = None
        self.active: dict[str, dict] = {}
        self.active_hypothetical: dict[str, dict] = {}
        self.counters = {
            "decision_packets": 0, "admitted_episodes": 0,
            "bid_records": 0, "outcome_records": 0,
            "gate_results": 0, "evidence_manifests": 0,
            "decision_events": 0,
            "no_trade_plans": 0, "hypothetical_bid_records": 0,
            "hypothetical_outcome_records": 0,
        }
        self._recover_active()
        self.replay_writer = replay.BoundedReplayPersistence(
            max_pending=int(collector["replay_queue_max_pending"]))
        self.status = self._status("ARMED_MARKET_CLOSED", market_window=False)
        self._write_status(force=True)

    def _status(self, state: str, *, market_window: bool, detail: str | None = None) -> dict:
        return {
            "schema_version": "entry-bid-collector-status-v1",
            "config_version": self.config["config_version"],
            "config_hash": self.config["config_hash"],
            "collector_version": COLLECTOR_VERSION,
            "enabled": True,
            "state": state,
            "detail": detail,
            "market_window": bool(market_window),
            "execution_authority": False,
            "guard_access": False,
            "formal_cohort_eligible": False,
            "collection_role": "PRELOCK_DRY_RUN",
            "cohorts_locked": False,
            "cost_model_status": self.config["outcome"]["cost_model_status"],
            "symbol": self.config["symbol"],
            "active_decisions": len(self.active),
            "active_hypothetical_decisions": len(self.active_hypothetical),
            "replay_persistence_pending": self.replay_writer.pending,
            "universe_as_of": self.last_universe_as_of,
            "universe_error": self.universe_refresh_error,
            "counters": dict(self.counters),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _write_status(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_status_write < 30.0:
            return
        temporary = self.status_path.with_suffix(self.status_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.status, sort_keys=True, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(self.status_path)
        self.last_status_write = time.monotonic()

    def public_status(self) -> dict:
        return deepcopy(self.status)

    def regime_inputs_snapshot(self) -> dict | None:
        """Return the latest causal inputs without another provider read."""
        return deepcopy(self.latest_regime_inputs)

    def _recover_active(self) -> None:
        decisions = {
            row.get("decision_id"): row for row in fe._iter_jsonl(self.decision_path)
            if row.get("decision") in {"CALL", "PUT"}
        }
        admitted = {
            row.get("decision_id") for row in fe._iter_jsonl(self.episode_path)
            if row.get("admitted") is True
        }
        terminal = {
            row.get("decision_id") for row in fe._iter_jsonl(self.outcome_path)
            if row.get("status") in {
                "FINAL", "UNLABELABLE_NO_EXECUTABLE_BIDS",
                "UNLABELABLE_INSUFFICIENT_COVERAGE",
            }
        }
        now = datetime.now(timezone.utc)
        for decision_id in admitted - terminal:
            packet = decisions.get(decision_id)
            if not packet:
                continue
            tracking = self._tracking_decision(packet)
            if _utc(tracking["entry_quote_observed_at"]) + timedelta(
                    minutes=tracking["outcome_horizon_minutes"] + 5) >= now:
                self.active[decision_id] = tracking
        plans = {
            row.get("decision_id"): row for row in fe._iter_jsonl(
                self.no_trade_plan_path)
            if row.get("status") == "TRACKABLE"
        }
        hypothetical_terminal = {
            row.get("decision_id") for row in fe._iter_jsonl(
                self.no_trade_outcome_path)
            if row.get("status") in {
                "FINAL", "UNLABELABLE_NO_EXECUTABLE_BIDS",
                "UNLABELABLE_INSUFFICIENT_COVERAGE",
            }
        }
        for decision_id in admitted - hypothetical_terminal:
            plan = plans.get(decision_id)
            if not plan:
                continue
            tracking = replay.tracking_decision_from_plan(plan)
            if _utc(tracking["entry_quote_observed_at"]) + timedelta(
                    minutes=tracking["outcome_horizon_minutes"] + 5) >= now:
                self.active_hypothetical[decision_id] = {
                    "tracking": tracking, "plan": plan}

    def _tracking_decision(self, packet: dict) -> dict:
        contract = packet["selected_contract"]
        outcome = self.config["outcome"]
        return {
            "config_version": packet["config_version"],
            "config_hash": packet["config_hash"],
            "decision_id": packet["decision_id"],
            "cohort_id": packet["cohort_id"],
            "option_symbol": contract["option_symbol"],
            "entry_quote_observed_at": contract["quote_observed_at"],
            "entry_ask": contract["ask"],
            "target_bid": packet["frozen_target"]["price"],
            "stop_bid": packet["frozen_invalidation"]["price"],
            "outcome_horizon_minutes": int(outcome["outcome_horizon_minutes"])
                if "outcome_horizon_minutes" in outcome
                else int(self.config["episode"]["outcome_horizon_minutes"]),
            "bid_poll_interval_seconds": float(outcome["bid_poll_interval_seconds"]),
            "max_fresh_gap_seconds": float(outcome["max_fresh_gap_seconds"]),
            "minimum_coverage_fraction": float(outcome["minimum_coverage_fraction"]),
            "return_horizons_minutes": list(outcome["return_horizons_minutes"]),
            "cost_model": dict(outcome["cost_model"]),
        }

    def _workup_path(self, market_date: str) -> Path:
        return ROOT / "runs" / f"{self.config['symbol']}_{market_date}.json"

    def _cached_universe(self, market_date: str) -> list[dict]:
        path = self._workup_path(market_date)
        if not path.is_file():
            return []
        payload = _load_json(path)
        self.last_universe_as_of = payload.get("as_of")
        lo, hi = (int(self.config["execution"]["dte_min"]),
                  int(self.config["execution"]["dte_max"]))
        return [row for row in payload.get("contracts") or []
                if row.get("dte") is not None and lo <= int(row["dte"]) <= hi]

    def _maybe_refresh_universe(self, now_et: datetime) -> None:
        market_date = now_et.date().isoformat()
        if self._cached_universe(market_date):
            self.universe_refresh_error = None
            return
        start_h, start_m = map(int, self.config["universe"]["refresh_after_et"].split(":"))
        if now_et.weekday() >= 5 or now_et.time() < wall_time(start_h, start_m):
            return
        retry_seconds = float(self.config["universe"]["refresh_retry_seconds"])
        if (self.universe_refresh_attempt_date == market_date
                and self.universe_refresh_attempt_at is not None
                and (now_et - self.universe_refresh_attempt_at).total_seconds() < retry_seconds):
            return
        self.universe_refresh_attempt_date = market_date
        self.universe_refresh_attempt_at = now_et
        try:
            from workup_api import do_workup
            do_workup(
                self.config["symbol"], int(self.config["execution"]["dte_min"]),
                int(self.config["execution"]["dte_max"]),
                float(self.config["universe"]["budget_usd"]),
            )
            if not self._cached_universe(market_date):
                raise RuntimeError("daily workup contains no 0-2 DTE contracts")
            self.universe_refresh_error = None
        except Exception as exc:
            self.universe_refresh_error = f"{type(exc).__name__}: {str(exc)[:160]}"

    def _underlying_inputs(self, *, session_open_et: datetime,
                           session_close_et: datetime, now: datetime) -> tuple[list[dict], dict]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        symbol = self.config["symbol"]
        feed = DataFeed.SIP if self.feed == "sip" else DataFeed.IEX
        open_utc = session_open_et.astimezone(timezone.utc)
        end_utc = min(now, session_close_et.astimezone(timezone.utc))
        request = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
            start=open_utc, end=end_utc + timedelta(minutes=1), feed=feed)
        raw = self.stock_data.get_stock_bars(request).data.get(symbol) or []
        bars = []
        for bar in raw:
            completed_at = bar.timestamp + timedelta(minutes=1)
            if completed_at > now:
                continue
            minute = int((bar.timestamp - open_utc).total_seconds() // 60)
            if minute < 0:
                continue
            bars.append({
                "t": minute, "open": float(bar.open), "high": float(bar.high),
                "low": float(bar.low), "close": float(bar.close),
                "volume": float(getattr(bar, "volume", 0) or 0),
                "observed_at": completed_at.astimezone(timezone.utc).isoformat(),
            })
        bars.sort(key=lambda row: row["t"])

        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        market_date = session_open_et.astimezone(et).date()
        prior_regime_sessions = self._regime_prior_sessions(
            market_date=market_date, session_open_utc=open_utc, feed=feed, et=et)
        daily_request = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=now - timedelta(days=14), end=now + timedelta(minutes=1), feed=feed)
        days = self.stock_data.get_stock_bars(daily_request).data.get(symbol) or []
        prior = [bar for bar in days if bar.timestamp.astimezone(et).date() < market_date]
        prior_day_high = float(prior[-1].high) if prior else None
        prior_day_low = float(prior[-1].low) if prior else None

        premarket_start = datetime.combine(market_date, wall_time(4, 0), tzinfo=et)
        premarket_request = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
            start=premarket_start.astimezone(timezone.utc), end=open_utc, feed=feed)
        premarket = self.stock_data.get_stock_bars(premarket_request).data.get(symbol) or []
        context = {
            "prior_day_high": prior_day_high,
            "prior_day_low": prior_day_low,
            "premarket_high": max((float(bar.high) for bar in premarket), default=None),
            "premarket_low": min((float(bar.low) for bar in premarket), default=None),
            "session_vwap": intelligence.session_vwap(bars),
            "underlying_spot": bars[-1]["close"] if bars else None,
            "underlying_observed_at": bars[-1].get("observed_at") if bars else None,
            "underlying_received_at": now.isoformat(),
            "underlying_source": f"alpaca_stock_bars:{self.feed}",
            "regime_prior_session_minute_bars": prior_regime_sessions,
            "regime_prior_session_error": self.regime_prior_session_error,
        }
        return bars, context

    def _regime_prior_sessions(self, *, market_date, session_open_utc: datetime,
                               feed, et) -> list[list[dict]]:
        """Cache prior RTH bars for advisory ATR percentile history only."""
        if self.regime_prior_session_date == market_date:
            return self.regime_prior_session_bars
        self.regime_prior_session_date = market_date
        self.regime_prior_session_bars = []
        self.regime_prior_session_error = None
        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            start_et = datetime.combine(
                market_date - timedelta(days=10), wall_time(9, 30), tzinfo=et)
            request = StockBarsRequest(
                symbol_or_symbols=self.config["symbol"], timeframe=TimeFrame.Minute,
                start=start_et.astimezone(timezone.utc), end=session_open_utc, feed=feed)
            raw = self.stock_data.get_stock_bars(request).data.get(self.config["symbol"]) or []
            sessions: dict[str, list[dict]] = {}
            for bar in raw:
                stamp = bar.timestamp.astimezone(et)
                session_date = stamp.date()
                if session_date >= market_date or session_date.weekday() >= 5:
                    continue
                minute = int((stamp - datetime.combine(
                    session_date, wall_time(9, 30), tzinfo=et)).total_seconds() // 60)
                if not 0 <= minute < 390:
                    continue
                sessions.setdefault(session_date.isoformat(), []).append({
                    "t": minute, "open": float(bar.open), "high": float(bar.high),
                    "low": float(bar.low), "close": float(bar.close),
                    "volume": float(getattr(bar, "volume", 0) or 0),
                })
            self.regime_prior_session_bars = [
                sorted(session, key=lambda row: row["t"])
                for _date, session in sorted(sessions.items())
            ]
        except Exception as exc:
            self.regime_prior_session_error = f"{type(exc).__name__}: {str(exc)[:160]}"
        return self.regime_prior_session_bars

    def _live_contracts(self, *, side: str, market_date: str, received_at: datetime,
                        underlying_spot: float | None = None,
                        underlying_observed_at: Any = None,
                        underlying_received_at: Any = None) -> dict:
        from alpaca.data.enums import OptionsFeed
        from alpaca.data.requests import OptionSnapshotRequest

        base = self._cached_universe(market_date)
        right = "C" if side == "CALL" else "P"
        candidates = [row for row in base
                      if str(row.get("type") or "").upper() in {right, side}
                      and int(row.get("oi") or 0) >= int(
                          self.config["execution"]["min_open_interest"])]
        if not candidates:
            return {"selected": None, "rejected": [], "eligible_count": 0,
                    "source_status": "MISSING", "reason": "fresh_0_2_dte_universe_missing"}
        snapshots = {}
        try:
            for start in range(0, len(candidates), 100):
                symbols = [row["symbol"] for row in candidates[start:start + 100]]
                response = self.option_data.get_option_snapshot(
                    OptionSnapshotRequest(
                        symbol_or_symbols=symbols, feed=OptionsFeed.OPRA))
                snapshots.update(response or {})
        except Exception:
            return {"selected": None, "rejected": [], "eligible_count": 0,
                    "source_status": "UNAVAILABLE", "reason": "alpaca_option_snapshot_unavailable"}
        contracts = []
        for row in candidates:
            snapshot = snapshots.get(row["symbol"])
            quote = getattr(snapshot, "latest_quote", None)
            greeks = getattr(snapshot, "greeks", None)
            daily = getattr(snapshot, "daily_bar", None)
            contracts.append(contract_data.assemble_contract(
                chain=row, chain_observed_at=self.last_universe_as_of,
                snapshot={
                    "bid": getattr(quote, "bid_price", None),
                    "ask": getattr(quote, "ask_price", None),
                    "quote_observed_at": getattr(quote, "timestamp", None),
                    "received_at": received_at,
                    "delta": getattr(greeks, "delta", None),
                    # Recorded only as an ignored artifact; never used.
                    "snapshot_volume": getattr(daily, "volume", None),
                },
                underlying={
                    "spot": underlying_spot,
                    "observed_at": underlying_observed_at,
                    "received_at": underlying_received_at or received_at,
                    "source": f"alpaca_stock_bars:{self.feed}",
                },
                decided_at=received_at,
                config=self.config["contract_assembly"],
            ))
        return intelligence.select_contract(
            side=side, contracts=contracts, decided_at=received_at,
            rules=self.config["execution"])

    def _direction_rules(self, side: str) -> dict:
        return {**self.config["direction"]["shared"],
                **self.config["direction"][side]}

    def _evaluate_minute(self, *, session_open_et: datetime,
                         session_close_et: datetime, now: datetime) -> None:
        bars, context = self._underlying_inputs(
            session_open_et=session_open_et, session_close_et=session_close_et, now=now)
        now_minute = int((now - session_open_et.astimezone(timezone.utc)).total_seconds() // 60)
        # Publish one immutable snapshot for other research consumers. The
        # runner reclassifies it directly instead of invoking the legacy HMM
        # or duplicating the collector's provider calls.
        self.latest_regime_inputs = {
            "minute_bars": deepcopy(bars),
            "now_minute": now_minute,
            "prior_session_minute_bars": deepcopy(
                context.get("regime_prior_session_minute_bars") or []),
            "observed_at": bars[-1].get("observed_at") if bars else None,
        }
        try:
            regime_tag = regime_layer_v0.classify_regime(
                minute_bars=bars, now_minute=now_minute,
                prior_session_minute_bars=context.get(
                    "regime_prior_session_minute_bars"),
            )
        except Exception as exc:
            # Advisory instrumentation must never become an admission gate.
            regime_tag = regime_layer_v0.unknown_regime(
                completed_bar_count=0,
                reasons=[f"regime_classifier_error:{type(exc).__name__}"],
            )
        setups = {}
        provisionals = {}
        for side in ("CALL", "PUT"):
            setup = intelligence.evaluate_reversal_setup(
                symbol=self.config["symbol"], side=side, minute_bars=bars,
                now_minute=now_minute, context=context,
                rules=self._direction_rules(side))
            provisional_selection = {
                "selected": None, "rejected": [], "eligible_count": 0,
                "source_status": "MISSING",
                "reason": "execution_not_evaluated_episode_admission_pending",
            }
            provisional = intelligence.build_decision_packet(
                cohort_document=self.cohorts[side], setup=setup,
                contract_selection=provisional_selection,
                observed_at=now, decided_at=now,
                config_version=self.config["config_version"],
                config_hash=self.config["config_hash"])
            setups[side] = setup
            provisionals[side] = provisional

        qualified_sides = {
            side for side, setup in setups.items()
            if episode_integrity.has_qualified_reversal(setup)
        }
        if qualified_sides == {"CALL", "PUT"}:
            candidates = [
                {
                    "config_version": self.config["config_version"],
                    "config_hash": self.config["config_hash"],
                    "episode_key": provisionals[side].episode_key,
                    "decision_id": provisionals[side].decision_id,
                    "cohort_id": provisionals[side].cohort_id,
                    "symbol": provisionals[side].symbol,
                    "side": side,
                    "session_date": session_open_et.date().isoformat(),
                    "decided_at": now.isoformat(),
                    "episode_kind": "REVERSAL_CANDIDATE",
                    "admitted": False,
                    "regime_tag": regime_tag,
                }
                for side in ("CALL", "PUT")
            ]
            episode_integrity.quarantine_cross_side_double_qualification(
                candidates,
                quarantine_path=self.episode_quarantine_path,
                integrity_event_path=self.integrity_event_path,
                detected_at=now,
                source_collector_version=COLLECTOR_VERSION,
            )

        for side in ("CALL", "PUT"):
            setup = setups[side]
            provisional = provisionals[side]
            admission = None
            has_qualified_reversal = episode_integrity.has_qualified_reversal(setup)
            if has_qualified_reversal:
                candidate = {
                    "config_version": self.config["config_version"],
                    "config_hash": self.config["config_hash"],
                    "episode_key": provisional.episode_key,
                    "decision_id": provisional.decision_id,
                    "cohort_id": provisional.cohort_id,
                    "symbol": provisional.symbol, "side": side,
                    "session_date": session_open_et.date().isoformat(),
                    "decided_at": now.isoformat(),
                    "episode_kind": "REVERSAL_CANDIDATE",
                    "regime_tag": regime_tag,
                }
                admission = episode_research.admit_episode(
                    candidate, existing=list(fe._iter_jsonl(self.episode_path)),
                    outcome_horizon_minutes=int(
                        self.config["episode"]["outcome_horizon_minutes"]),
                    cooldown_minutes=int(self.config["episode"]["cooldown_minutes"]))
                episode_research.append_episode(admission, self.episode_path)
            if admission and admission["admitted"]:
                # Admission intentionally precedes option polling, so rejected
                # and taken decisions share one non-overlapping episode ledger.
                selection = self._live_contracts(
                    side=side, market_date=session_open_et.date().isoformat(),
                    received_at=now,
                    underlying_spot=context.get("underlying_spot"),
                    underlying_observed_at=context.get("underlying_observed_at"),
                    underlying_received_at=context.get("underlying_received_at"))
            elif admission:
                selection = {
                    "selected": None, "rejected": [], "eligible_count": 0,
                    "source_status": "MISSING",
                    "reason": "execution_not_evaluated_episode_not_admitted",
                }
            else:
                selection = {
                    "selected": None, "rejected": [], "eligible_count": 0,
                    "source_status": "UNAVAILABLE",
                    "reason": "execution_not_evaluated_direction_not_qualified",
                }
            packet = intelligence.build_decision_packet(
                cohort_document=self.cohorts[side], setup=setup,
                contract_selection=selection, observed_at=now, decided_at=now,
                config_version=self.config["config_version"],
                config_hash=self.config["config_hash"])
            payload = packet.model_dump(mode="json")
            if _append_unique(payload, self.decision_path, "decision_id"):
                self.counters["decision_packets"] += 1
            gate_results = replay.build_gate_results(
                packet=payload, setup=setup, selection=selection,
                config=self.config, evaluated_at=now)
            plan = None
            if admission and admission["admitted"]:
                self.counters["admitted_episodes"] += 1
                if packet.decision in {"CALL", "PUT"}:
                    tracking = self._tracking_decision(payload)
                    self.active[packet.decision_id] = tracking
                    contract = payload["selected_contract"]
                    entry_record = bid_capture.build_bid_record(
                        decision_id=packet.decision_id, cohort_id=packet.cohort_id,
                        option_symbol=contract["option_symbol"],
                        observed_at=contract["quote_observed_at"], received_at=now,
                        bid=contract["bid"], ask=contract["ask"],
                        max_quote_age_seconds=float(
                            self.config["execution"]["max_quote_age_seconds"]),
                        config_version=self.config["config_version"],
                        config_hash=self.config["config_hash"])
                    if bid_capture.append_bid_record(entry_record, self.bid_path):
                        self.counters["bid_records"] += 1
                else:
                    plan = replay.build_no_trade_tracking_plan(
                        packet=payload, side=side, selection=selection,
                        config=self.config, created_at=now)
                    self.counters["no_trade_plans"] += 1
                    if plan.status == "TRACKABLE":
                        tracking = replay.tracking_decision_from_plan(plan)
                        self.active_hypothetical[packet.decision_id] = {
                            "tracking": tracking,
                            "plan": plan.model_dump(mode="json")}
                        contract = plan.selected_contract or {}
                        entry_record = bid_capture.build_bid_record(
                            decision_id=packet.decision_id,
                            cohort_id=packet.cohort_id,
                            option_symbol=contract["option_symbol"],
                            observed_at=contract["quote_observed_at"],
                            received_at=now, bid=contract["bid"], ask=contract["ask"],
                            max_quote_age_seconds=float(
                                self.config["execution"]["max_quote_age_seconds"]),
                            config_version=self.config["config_version"],
                            config_hash=self.config["config_hash"])
                        if bid_capture.append_bid_record(
                                entry_record, self.no_trade_bid_path):
                            self.counters["hypothetical_bid_records"] += 1
            self.replay_writer.submit(
                replay.persist_decision_bundle,
                packet=payload, setup=setup, selection=selection,
                config=self.config, gate_results=gate_results,
                decision_path=self.decision_path,
                gate_result_path=self.gate_result_path,
                evidence_source_path=self.evidence_source_path,
                evidence_manifest_path=self.evidence_manifest_path,
                decision_event_path=self.decision_event_path,
                no_trade_plan_path=self.no_trade_plan_path,
                episode_record_id=(
                    admission.get("episode_record_id") if admission else None),
                no_trade_plan=plan, created_at=now)
            self.counters["gate_results"] += len(gate_results)
            self.counters["evidence_manifests"] += 1
            self.counters["decision_events"] += 1

    def _capture_active(self, *, now: datetime, market_active: bool) -> None:
        for decision_id, decision in list(self.active.items()):
            terminal = (_utc(decision["entry_quote_observed_at"]) + timedelta(
                minutes=int(decision["outcome_horizon_minutes"])))
            if market_active and now <= terminal:
                try:
                    record = bid_capture.capture_latest_option_quote(
                        option_data=self.option_data, decision_id=decision_id,
                        cohort_id=decision["cohort_id"],
                        option_symbol=decision["option_symbol"], received_at=now,
                        max_quote_age_seconds=float(
                            self.config["execution"]["max_quote_age_seconds"]),
                        config_version=self.config["config_version"],
                        config_hash=self.config["config_hash"])
                except Exception:
                    record = bid_capture.build_bid_record(
                        decision_id=decision_id, cohort_id=decision["cohort_id"],
                        option_symbol=decision["option_symbol"], observed_at=now,
                        received_at=now, bid=None, ask=None,
                        max_quote_age_seconds=float(
                            self.config["execution"]["max_quote_age_seconds"]),
                        config_version=self.config["config_version"],
                        config_hash=self.config["config_hash"], source_available=False)
                if bid_capture.append_bid_record(record, self.bid_path):
                    self.counters["bid_records"] += 1
            outcome = bid_capture.evaluate_outcome(
                decision, bid_capture.records_for_decision(decision_id, self.bid_path),
                evaluated_at=now)
            if bid_capture.append_outcome(outcome, self.outcome_path):
                self.counters["outcome_records"] += 1
            if outcome["status"] in {
                    "FINAL", "UNLABELABLE_NO_EXECUTABLE_BIDS",
                    "UNLABELABLE_INSUFFICIENT_COVERAGE"}:
                self.active.pop(decision_id, None)

    def _capture_hypothetical(self, *, now: datetime,
                              market_active: bool) -> None:
        """Capture rejected-decision paths in the physically separate store."""
        for decision_id, item in list(self.active_hypothetical.items()):
            decision, plan = item["tracking"], item["plan"]
            terminal = (_utc(decision["entry_quote_observed_at"]) + timedelta(
                minutes=int(decision["outcome_horizon_minutes"])))
            if market_active and now <= terminal:
                try:
                    record = bid_capture.capture_latest_option_quote(
                        option_data=self.option_data, decision_id=decision_id,
                        cohort_id=decision["cohort_id"],
                        option_symbol=decision["option_symbol"], received_at=now,
                        max_quote_age_seconds=float(
                            self.config["execution"]["max_quote_age_seconds"]),
                        config_version=self.config["config_version"],
                        config_hash=self.config["config_hash"])
                except Exception:
                    record = bid_capture.build_bid_record(
                        decision_id=decision_id, cohort_id=decision["cohort_id"],
                        option_symbol=decision["option_symbol"], observed_at=now,
                        received_at=now, bid=None, ask=None,
                        max_quote_age_seconds=float(
                            self.config["execution"]["max_quote_age_seconds"]),
                        config_version=self.config["config_version"],
                        config_hash=self.config["config_hash"], source_available=False)
                if bid_capture.append_bid_record(record, self.no_trade_bid_path):
                    self.counters["hypothetical_bid_records"] += 1
            outcome = bid_capture.evaluate_outcome(
                decision,
                bid_capture.records_for_decision(
                    decision_id, self.no_trade_bid_path),
                evaluated_at=now)
            hypothetical = replay.decorate_hypothetical_outcome(outcome, plan)
            self.replay_writer.submit(
                replay.append_hypothetical_outcome,
                hypothetical, self.no_trade_outcome_path)
            self.counters["hypothetical_outcome_records"] += 1
            if outcome["status"] in {
                    "FINAL", "UNLABELABLE_NO_EXECUTABLE_BIDS",
                    "UNLABELABLE_INSUFFICIENT_COVERAGE"}:
                self.active_hypothetical.pop(decision_id, None)

    def tick(self, *, market_active: bool, session_open_et: datetime | None,
             session_close_et: datetime | None, now: datetime | None = None) -> dict:
        now = _utc(now or datetime.now(timezone.utc))
        try:
            self.replay_writer.raise_if_failed()
            from zoneinfo import ZoneInfo
            now_et = now.astimezone(ZoneInfo("America/New_York"))
            self._maybe_refresh_universe(now_et)
            if market_active and session_open_et and session_close_et:
                minute_key = now.replace(second=0, microsecond=0).isoformat()
                if minute_key != self.last_decision_minute:
                    self._evaluate_minute(
                        session_open_et=session_open_et,
                        session_close_et=session_close_et, now=now)
                    self.last_decision_minute = minute_key
                self._capture_active(now=now, market_active=True)
                self._capture_hypothetical(now=now, market_active=True)
                self.status = self._status("ACTIVE_RTH_CAPTURE", market_window=True)
            else:
                self._capture_active(now=now, market_active=False)
                self._capture_hypothetical(now=now, market_active=False)
                self.status = self._status("ARMED_MARKET_CLOSED", market_window=False)
        except Exception as exc:
            self.status = self._status(
                "DEGRADED_FAIL_CLOSED", market_window=market_active,
                detail=f"{type(exc).__name__}: {str(exc)[:180]}")
        self._write_status()
        return self.public_status()
