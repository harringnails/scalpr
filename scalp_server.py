"""
SCALP SERVER v2 — the platform backend
=======================================
Wraps the ladder engine in a local web app: enter trades in the browser,
watch every guarded position live, and read the journal.

Setup:
    pip install alpaca-py fastapi uvicorn
    export ALPACA_API_KEY=...      # paper keys to start
    export ALPACA_SECRET_KEY=...

Run:
    python scalp_server.py                 # paper mode
Then open http://localhost:8420

Design notes:
  * Prices come from a polling loop (2x/second, batched) rather than a
    websocket — simpler, robust, and lets you add trades at any time.
    scalp_engine.py remains the tick-level websocket alternative.
  * The ratchet ladder is enforced server-side. The market read is
    advisory text only; it can never loosen the ladder.
"""

import argparse
import csv
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard_cache import DashboardCache
from health_events import HealthEventLog
from operational_store import OperationalStore
from alpaca_index_data import AlpacaIndexDataClient
from ivolatility_adapter import IVolatilityClient
from options_feature_store import OptionsFeatureStore
from unusual_whales_adapter import UnusualWhalesAdapter
from institutional_flow_store import InstitutionalFlowStore
from explanation_layer_v0 import (
    ExplanationService, build_evidence_brief, safe_raw_brief,
)
import scope_policy
import manual_scope_policy
from storage_maintenance import rotate_csv, storage_health

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (MarketOrderRequest, LimitOrderRequest,
                                    GetOptionContractsRequest, GetOrdersRequest)
from alpaca.trading.enums import (OrderSide, TimeInForce, ContractType,
                                  AssetStatus, AssetClass, QueryOrderStatus)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import (StockLatestTradeRequest, OptionLatestTradeRequest,
                                  StockLatestQuoteRequest, OptionLatestQuoteRequest,
                                  StockBarsRequest)
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

JOURNAL = Path("scalp_journal.csv")
DASHBOARD = Path(__file__).parent / "dashboard.html"
LOCK = threading.Lock()
POLL_SECONDS = 1.0

# Standing tick logger — independent of guarded trades, so it accumulates a
# real quote history for SPY (or whatever's added here) all the time, not
# just while a position is open. This is raw material for the "does the last
# couple minutes predict the next" question — not an answer to it.
TICK_LOG = Path("tick_log.csv")
TICK_SYMBOLS = ["SPY"]
TICK_SECONDS = 2.0
TICK_QUOTE_STALE_SECONDS = float(os.getenv("SCALPR_TICK_QUOTE_STALE_SECONDS", "30"))
TICK_ROTATE_BYTES = int(os.getenv("SCALPR_TICK_ROTATE_BYTES", str(32 * 1024 * 1024)))
TICK_ROTATE_CHECK_SECONDS = 60.0
# utc_time = local RECEIPT time; provider_ts = the feed's event time. Both are
# kept — receipt time is never used as a substitute for event time (see the
# timing contract in bar_builder.py). Rows written before provider_ts existed
# have it blank and are treated as 'unaudited', not silently trusted.
TICK_FIELDS = ["utc_time", "provider_ts", "symbol", "bid", "ask",
               "bid_size", "ask_size", "mid", "spread"]


def _tick_quote_row(symbol, quote, *, received_at=None,
                    stale_seconds=TICK_QUOTE_STALE_SECONDS):
    """Normalize one quote or reject it when event time is unusable.

    A latest-quote endpoint can keep returning the final market event for hours.
    Repeating that event at receipt time fabricates apparent observations, so
    capture is allowed only while the provider timestamp is present and fresh.
    """
    received = received_at or datetime.now(timezone.utc)
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    provider_ts = getattr(quote, "timestamp", None)
    if provider_ts is None:
        return None, "MISSING_PROVIDER_TIMESTAMP", None
    if provider_ts.tzinfo is None:
        provider_ts = provider_ts.replace(tzinfo=timezone.utc)
    age_seconds = (received - provider_ts).total_seconds()
    if age_seconds > float(stale_seconds):
        return None, "STALE_PROVIDER_TIMESTAMP", round(age_seconds, 3)
    bid, ask = float(getattr(quote, "bid_price", 0) or 0), float(
        getattr(quote, "ask_price", 0) or 0)
    if not bid and not ask:
        return None, "EMPTY_QUOTE", round(age_seconds, 3)
    mid = (bid + ask) / 2 if bid and ask else (bid or ask)
    return {
        "utc_time": received.astimezone(timezone.utc).isoformat(),
        "provider_ts": provider_ts.astimezone(timezone.utc).isoformat(),
        "symbol": str(symbol).upper(),
        "bid": bid, "ask": ask,
        "bid_size": float(getattr(quote, "bid_size", 0) or 0),
        "ask_size": float(getattr(quote, "ask_size", 0) or 0),
        "mid": round(mid, 4),
        "spread": round(ask - bid, 4) if bid and ask else "",
    }, None, round(age_seconds, 3)


def _migrate_tick_log():
    """One-time, idempotent upgrade of a tick log written before provider_ts.
    Old rows keep their data; provider_ts comes back blank (→ unaudited)."""
    if not TICK_LOG.exists():
        return
    with TICK_LOG.open(newline="") as f:
        reader = csv.DictReader(f)
        first = reader.fieldnames
        if first and set(first) == set(TICK_FIELDS):
            return
        rows = list(reader)
    with TICK_LOG.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TICK_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in TICK_FIELDS})
    log(f"tick log migrated to timestamp-audited schema ({len(rows)} rows preserved)")

from precheck import SNAPSHOT_KEYS, flatten_signals as _flatten_signals

JOURNAL_FIELDS = (["utc_time", "mode", "symbol", "qty", "entry", "exit",
                    "peak_pct", "realized_pct", "reason", "scope_class",
                    "scope_version", "entry_source"]
                  + [f"entry_{k}" for k in SNAPSHOT_KEYS]
                  + [f"exit_{k}" for k in SNAPSHOT_KEYS])


def _migrate_journal():
    """One-time, idempotent upgrade of an older journal file (pre entry/exit
    signal columns) to the current schema. Old rows keep their trade data;
    the new signal columns come back blank for them since that data was
    never captured — nothing is lost or guessed."""
    if not JOURNAL.exists():
        return
    with JOURNAL.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows or set(rows[0].keys()) == set(JOURNAL_FIELDS):
        return
    with JOURNAL.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in JOURNAL_FIELDS})
    log(f"journal migrated to signal-tracking schema ({len(rows)} existing trades preserved)")


def _provider_capture_window(now=None):
    """Pure ET schedule for bounded provider capture workers."""
    try:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
    except Exception:
        ET = timezone.utc
    current = now or datetime.now(ET)
    if current.tzinfo is None:
        current = current.replace(tzinfo=ET)
    current = current.astimezone(ET)
    weekday = current.weekday() < 5
    minute = current.hour * 60 + current.minute
    return {
        "now_et": current,
        "flow_active": weekday and 9 * 60 + 30 <= minute <= 16 * 60 + 15,
        "ivol_eod_due": weekday and minute >= 16 * 60 + 20,
        "market_date": current.date().isoformat(),
    }


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


ALLOW_DASHBOARD_WITHOUT_ALPACA = (
    os.getenv("SCALPR_ALLOW_LOCAL_DASHBOARD", "").strip() == "1"
)


def load_keys():
    """Use environment or Keychain-loaded env only; never write plaintext keys."""
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if key and secret:
        return key, secret
    raise RuntimeError(
        "Alpaca paper credentials are unavailable. Source load_keychain_env.sh "
        "to load the Keychain-backed env vars, or export ALPACA_API_KEY / "
        "ALPACA_SECRET_KEY in the environment."
    )


def _require_flat_paper_account(trading):
    """Fail closed before creating in-memory Guard state on a fresh process."""
    if ALLOW_DASHBOARD_WITHOUT_ALPACA:
        print(
            "WARNING: Alpaca verification bypassed by SCALPR_ALLOW_LOCAL_DASHBOARD=1. "
            "Dashboard is running in local-only mode."
        )
        return 0
    try:
        positions = trading.get_all_positions()
    except Exception as exc:
        detail = str(exc).lower()
        if "401" in detail or "unauthor" in detail or "auth" in detail:
            raise RuntimeError(
                "Startup blocked: Alpaca paper authentication failed. "
                "Check ALPACA_API_KEY / ALPACA_SECRET_KEY and the Keychain item "
                "loaded by load_keychain_env.sh."
            ) from exc
        raise RuntimeError(
            "Startup blocked: Alpaca paper holdings could not be verified. "
            "Check network access and the Alpaca API response."
        ) from exc
    if positions:
        symbols = ", ".join(str(getattr(p, "symbol", "unknown")) for p in positions[:5])
        raise RuntimeError(
            f"Startup blocked: Alpaca reports {len(positions)} open position(s) "
            f"({symbols}). A fresh process has no Guard state for them."
        )
    return 0


class Guard:
    def __init__(self, cfg, entry, qty, entry_signals=None):
        self.cfg = cfg
        self.symbol = cfg["symbol"]
        self.kind = cfg.get("type", "stock")
        self.entry = entry
        self.qty = qty
        self.entry_signals = entry_signals  # run_precheck() snapshot taken at fill time, or None
        self.scope_class = cfg.get("scope_class", manual_scope_policy.SCOPE_VALIDATED)
        self.scope_version = cfg.get("scope_version", scope_policy.SCOPE_VERSION)
        self.entry_source = cfg.get("entry_source", "automated_or_legacy")
        self.ladder = sorted(cfg["ladder"], key=lambda r: r["at"])
        self.peak = 0.0
        self.peak_time = time.time()
        self.stall_seconds = cfg.get("stall_seconds", 0)
        self.stall_min_profit = cfg.get("stall_min_profit", 20)
        self.grace_seconds = cfg.get("grace_seconds", 60)
        self.confirm_ticks = max(1, int(cfg.get("confirm_ticks", 2)))
        self.opened_at = time.time()
        self.breach = 0
        self.last = entry
        self.last_update = None
        self.quote_status = "AWAITING_EXECUTABLE_BID" if self.kind == "option" else "AWAITING_BID"
        self.quote_detail = None
        self.done = False
        self.paused = False   # when True the ratchet does NOT fire exits (no protection)
        self.opened = datetime.now(timezone.utc).isoformat()
        self.execution_lock = threading.Lock()
        self.climb = None
        self.climb_next_poll = 0.0
        self.incubation_trade_id = None

    def resume(self):
        """Re-engage the guard, re-armed from the CURRENT price: the peak resets to
        the current profit and a fresh grace period starts, so a stale high peak
        can't trigger an instant exit on resume."""
        if self.quote_status != "OK" or self.last_update is None:
            raise ValueError("cannot re-engage guard without a current executable bid")
        self.paused = False
        self.peak = (self.last - self.entry) / self.entry * 100
        self.peak_time = time.time()
        self.opened_at = time.time()
        self.breach = 0

    def tolerance(self):
        tol = self.ladder[0]["tol"]
        for rung in self.ladder:
            if self.peak >= rung["at"]:
                tol = rung["tol"]
        return tol

    def on_price(self, price):
        if price is None or float(price) <= 0:
            self.mark_quote_unavailable("NO_BID")
            return None
        price = float(price)
        self.last = price
        self.last_update = time.time()
        self.quote_status = "OK"
        self.quote_detail = None
        if self.paused:            # guard disengaged: track price, never exit
            return None
        profit = (price - self.entry) / self.entry * 100
        if profit > self.peak:
            self.peak, self.peak_time = profit, time.time()
            self.breach = 0
            return None
        if time.time() - self.opened_at < self.grace_seconds:
            return None
        tol = self.tolerance()
        if profit <= self.peak - tol:
            self.breach += 1
            if self.breach < self.confirm_ticks:
                return None
            return (f"dip: {profit:+.3f}% is {self.peak - profit:.3f}% below "
                    f"peak {self.peak:+.3f}% (tol {tol}%, confirmed {self.breach}x)")
        self.breach = 0
        if (self.stall_seconds and self.peak >= self.stall_min_profit
                and time.time() - self.peak_time >= self.stall_seconds):
            return f"stall: {self.stall_seconds}s without a new peak above +{self.stall_min_profit}%"
        return None

    def mark_quote_unavailable(self, status="NO_BID", detail=None):
        """Expose degraded execution data without changing price, peak, or breach."""
        self.quote_status = status
        self.quote_detail = detail

    def rebase_after_add(self, entry, qty):
        """Adopt broker-confirmed cost/quantity without reopening exit grace."""
        self.entry, self.qty = float(entry), float(qty)
        self.peak = ((self.last - self.entry) / self.entry * 100
                     if self.quote_status == "OK" and self.last_update else 0.0)
        self.peak_time = time.time()
        self.breach = 0

    # Execution mode is fixed: the rungs are profit-ACTIVATION + allowed-GIVEBACK
    # tiers (as peak climbs, the tolerated giveback tightens), NOT staged scale-out
    # levels. A valid exit condition sells 100% of the remaining position in one
    # order (see Platform.sell → qty=guard.qty). Partial profit-taking is not
    # enabled. This holds for any contract count (1, 2, or many).
    EXECUTION_MODE = "WHOLE_POSITION_RATCHET"

    def snapshot(self):
        executable = self.quote_status == "OK" and self.last_update is not None
        profit = ((self.last - self.entry) / self.entry * 100) if executable else None
        tol = self.tolerance()
        return {
            "symbol": self.symbol, "type": self.kind, "entry": self.entry,
            "qty": self.qty, "last": self.last if executable else None,
            "profit": round(profit, 3) if profit is not None else None,
            "peak": round(self.peak, 3), "tolerance": tol,
            "sell_level_pct": round(self.peak - tol, 3),
            "stall_seconds": self.stall_seconds,
            "stall_min_profit": self.stall_min_profit,
            "opened": self.opened,
            "scope_class": self.scope_class,
            "scope_version": self.scope_version,
            "entry_source": self.entry_source,
            "execution_mode": self.EXECUTION_MODE,
            "sells_pct_on_exit": 100,   # whole remaining position, never partial
            "partial_profit_taking_enabled": False,
            "grace_left": max(0, round(self.grace_seconds - (time.time() - self.opened_at), 1)),
            "breach": self.breach, "confirm_ticks": self.confirm_ticks,
            "paused": self.paused,
            "age": round(time.time() - self.last_update, 1) if self.last_update else None,
            "quote_status": self.quote_status,
            "quote_detail": self.quote_detail,
            "execution_protected": bool(executable and not self.paused),
            "climb_add": ({
                "version": self.climb.get("version"),
                "status": self.climb.get("status"),
                "reason_codes": self.climb.get("reason_codes", []),
                "filled_adds": self.climb.get("position", {}).get("filled_adds", 0),
                "enabled": True,
            } if self.climb else {
                "enabled": bool(self.cfg.get("climb_adds", {}).get("enabled")),
                "status": ("MASTER_FLAG_OFF" if self.cfg.get("climb_adds", {}).get("enabled")
                           else "DISABLED"),
            }),
        }


class AsymmetricHardStopGuard(Guard):
    EXECUTION_MODE = "ASYMMETRIC_HARD_STOP_V1"

    def __init__(self, cfg, entry, qty, entry_signals=None):
        super().__init__(cfg, entry, qty, entry_signals=entry_signals)
        self.hard_stop_loss_pct = float(cfg.get("hard_stop_loss_pct", 25))
        self.hard_stop_profit_activation_pct = float(
            cfg.get("hard_stop_profit_activation_pct", 20))
        self.hard_stop_peak_giveback_pct = float(cfg.get("hard_stop_peak_giveback_pct", 10))
        self.hard_stop_quote_degradation_seconds = float(
            cfg.get("hard_stop_quote_degradation_seconds", 12))
        self.hard_stop_loss_cap_confirmation_ticks = max(
            1, int(cfg.get("hard_stop_loss_cap_confirmation_ticks", 2)))
        self.hard_stop_loss_breach = 0
        self.hard_stop_trail_breach = 0
        self.hard_stop_quote_bad_since = None

    def resume(self):
        """Re-engage without rebasing the risk state.

        The hard-stop path must not be bypassed by pause/resume, so resume only
        clears the paused flag. The current peak, opened_at, and breach counters
        stay intact.
        """
        if self.quote_status != "OK" or self.last_update is None:
            raise ValueError("cannot re-engage guard without a current executable bid")
        self.paused = False

    def mark_quote_unavailable(self, status="NO_BID", detail=None):
        super().mark_quote_unavailable(status, detail)
        now = time.time()
        if self.hard_stop_quote_bad_since is None:
            self.hard_stop_quote_bad_since = now
        if now - self.hard_stop_quote_bad_since >= self.hard_stop_quote_degradation_seconds:
            return (f"hard-stop: quote degraded for {self.hard_stop_quote_degradation_seconds:g}s "
                    f"without a fresh two-sided mark ({status})")
        return None

    def on_price(self, price):
        if price is None or float(price) <= 0:
            return self.mark_quote_unavailable("NO_BID")
        price = float(price)
        self.last = price
        self.last_update = time.time()
        self.quote_status = "OK"
        self.quote_detail = None
        self.hard_stop_quote_bad_since = None
        profit = (price - self.entry) / self.entry * 100
        if profit > self.peak:
            self.peak, self.peak_time = profit, time.time()
            self.hard_stop_loss_breach = 0
            self.hard_stop_trail_breach = 0
            return None
        if time.time() - self.opened_at < self.grace_seconds:
            return None
        if profit <= -self.hard_stop_loss_pct:
            self.hard_stop_loss_breach += 1
            if self.hard_stop_loss_breach < self.hard_stop_loss_cap_confirmation_ticks:
                return None
            return (f"hard-stop: loss cap {profit:+.3f}% is at or beyond "
                    f"-{self.hard_stop_loss_pct:g}% (confirmed {self.hard_stop_loss_breach}x)")
        self.hard_stop_loss_breach = 0
        if self.peak >= self.hard_stop_profit_activation_pct:
            giveback_floor = self.peak - self.hard_stop_peak_giveback_pct
            if profit <= giveback_floor:
                self.hard_stop_trail_breach += 1
                if self.hard_stop_trail_breach < self.confirm_ticks:
                    return None
                return (f"hard-stop trail: {profit:+.3f}% is below peak {self.peak:+.3f}% "
                        f"(giveback {self.hard_stop_peak_giveback_pct:g}%, confirmed "
                        f"{self.hard_stop_trail_breach}x)")
            self.hard_stop_trail_breach = 0
        return None

    def snapshot(self):
        out = super().snapshot()
        out.update({
            "execution_mode": self.EXECUTION_MODE,
            "risk_mode": "asymmetric_hard_stop_v1",
            "hard_stop_loss_pct": self.hard_stop_loss_pct,
            "hard_stop_profit_activation_pct": self.hard_stop_profit_activation_pct,
            "hard_stop_peak_giveback_pct": self.hard_stop_peak_giveback_pct,
            "hard_stop_quote_degradation_seconds": self.hard_stop_quote_degradation_seconds,
            "hard_stop_loss_cap_confirmation_ticks": self.hard_stop_loss_cap_confirmation_ticks,
        })
        return out


class Platform:
    _scheduler_started = False   # process-wide guard: snapshot scheduler starts once

    def __init__(self, live: bool, feed: str = "iex"):
        if live:
            raise RuntimeError("Live mode is physically blocked; Scalpr V2 is paper/shadow only.")
        key, secret = load_keys()
        self.live = live
        self.feed = feed  # "iex" (free) or "sip" (requires Algo Trader Plus)
        self.trading = TradingClient(key, secret, paper=not live)
        _require_flat_paper_account(self.trading)
        self.stock_data = StockHistoricalDataClient(key, secret)
        self.option_data = OptionHistoricalDataClient(key, secret)
        # Read-only native index values for SPX Wave shadow simulations. The
        # adapter exposes no order methods and is never used by Standard mode.
        self.index_data = AlpacaIndexDataClient(key, secret)
        self.guards = {}
        self.market_note = "warming up…"
        self.feed_error = None
        self.feed_startup_notice = None
        if self.feed == "sip":
            # A newly purchased plan can expose delayed SIP history before its
            # real-time grant reaches the current API identity. Never leave the
            # Guard/tick paths pointed at a feed that rejects latest quotes.
            try:
                probe = self.stock_data.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=["SPY"], feed=DataFeed.SIP))
                if not probe.get("SPY"):
                    raise RuntimeError("SIP latest-quote probe returned no SPY quote")
            except Exception as exc:
                self.feed = "iex"
                self.feed_startup_notice = (
                    "Trader Plus SIP was requested but its real-time grant is not active "
                    f"for these API credentials; Scalpr safely fell back to IEX ({str(exc)[:120]}).")
                log(self.feed_startup_notice)
        self.health = HealthEventLog()
        self.capture_guard_events = True
        self._last_reconcile = 0.0
        self._last_open_symbols = set()
        self._last_guard_cycle_at = None
        self._entry_enrichment_queue = queue.Queue()
        self.dashboard_cache = DashboardCache()
        self.store = None
        self.flow_adapter = None
        self.flow_store = None
        self.institutional_flow_ingestion_running = False
        self.institutional_flow_last_result = None
        self.institutional_flow_market_window = False
        self.options_client = None
        self.options_store = None
        self.options_capture_running = False
        self.options_capture_last_result = None
        self.entry_bid_collector = None
        try:
            import entry_bid_collector_v1
            self.entry_bid_collector_status = entry_bid_collector_v1.disabled_status()
        except Exception:
            self.entry_bid_collector_status = {
                "enabled": False, "state": "DISABLED_IMPORT_ERROR",
                "execution_authority": False, "guard_access": False,
                "formal_cohort_eligible": False,
            }
        self.session_market_date = None
        self.session_open_et = None
        self.session_close_et = None
        _migrate_journal()
        try:
            self.store = OperationalStore()
            self.store.sync_journal(JOURNAL)
        except Exception as e:
            # The SQLite mirror is an optional read accelerator. It must never
            # prevent the paper platform from starting or managing a Guard.
            log(f"operational store unavailable; CSV fallback active: {e}")
        self._start_cloudsql_mirror()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._automation_loop, daemon=True).start()
        threading.Thread(target=self._research_loop, daemon=True).start()
        threading.Thread(target=self._market_loop, daemon=True).start()
        threading.Thread(target=self._tick_log_loop, daemon=True).start()
        threading.Thread(target=self._provider_capture_loop, daemon=True).start()
        threading.Thread(target=self._entry_bid_collector_loop, daemon=True).start()
        # Start the daily snapshot scheduler exactly once per process. The guard
        # protects against a second Platform being constructed (reload, tests);
        # cross-process duplication is separately prevented by the writer's file
        # lock + idempotency. For multi-worker deployments, run one worker or set
        # SCALPR_DISABLE_SCHEDULER=1 on all but the designated one.
        if (not Platform._scheduler_started
                and os.getenv("SCALPR_DISABLE_SCHEDULER", "") != "1"):
            Platform._scheduler_started = True
            threading.Thread(target=self._snapshot_loop, daemon=True).start()
            threading.Thread(target=self._shadow_loop, daemon=True).start()
        else:
            log("snapshot/shadow schedulers not started (already running or disabled)")

    def _start_cloudsql_mirror(self):
        """Start the optional no-broker evidence worker under this process.

        restart.sh retires the prior PID before replacing the server. Starting
        here (rather than as a sibling shell job) keeps the worker alive for the
        same lifecycle as the long-running local service.
        """
        self.cloudsql_mirror_process = None
        if os.getenv("SCALPR_CLOUDSQL_MIRROR_ENABLED", "") != "1":
            return
        root = Path(__file__).resolve().parent
        python = root / ".venv/bin/python"
        worker = root / "cloudsql_mirror.py"
        profile = root / "v2_data/cloudsql_profile.json"
        if not (python.is_file() and worker.is_file() and profile.is_file()):
            self.health.record(
                "cloudsql_mirror", "DISABLED",
                detail="optional runtime, worker, or profile missing", force=True)
            return
        output = root / "v2_data/cloudsql_mirror.log"
        pid_path = root / "v2_data/cloudsql_mirror.pid"
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output.open("ab", buffering=0) as handle:
                process = subprocess.Popen(
                    [str(python), str(worker), "--loop", "--interval", "30",
                     "--max-records", "250"], cwd=str(root),
                    stdin=subprocess.DEVNULL, stdout=handle,
                    stderr=subprocess.STDOUT, start_new_session=True)
            pid_path.write_text(f"{process.pid}\n")
            self.cloudsql_mirror_process = process
            self.health.record(
                "cloudsql_mirror", "STARTED",
                fields={"pid": process.pid, "execution_authority": False}, force=True)
        except (OSError, subprocess.SubprocessError) as exc:
            self.health.record(
                "cloudsql_mirror", "ERROR", detail=type(exc).__name__, force=True)

    # ── trade lifecycle ──
    OCC_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")

    @staticmethod
    def _model_field(model, name, default=None):
        if isinstance(model, dict):
            return model.get(name, default)
        return getattr(model, name, default)

    @staticmethod
    def _enum_text(value):
        raw = getattr(value, "value", value)
        return str(raw or "").lower()

    def _validate_manual_equity_underlying(self, underlying):
        """Require an active, tradable Alpaca US-equity/ETF asset."""
        underlying = manual_scope_policy.validate_underlying(underlying)
        try:
            asset = self.trading.get_asset(underlying)
        except Exception as exc:
            raise manual_scope_policy.ManualScopeError(
                f"Alpaca could not validate {underlying} as an equity: {exc}"
            ) from exc
        asset_class = self._enum_text(self._model_field(asset, "asset_class",
                                                        self._model_field(asset, "class")))
        status = self._enum_text(self._model_field(asset, "status"))
        tradable = self._model_field(asset, "tradable", False)
        if asset_class != self._enum_text(AssetClass.US_EQUITY):
            raise manual_scope_policy.ManualScopeError(
                f"{underlying} is not an Alpaca US-equity/ETF underlying; index options remain blocked."
            )
        if status != self._enum_text(AssetStatus.ACTIVE) or tradable is not True:
            raise manual_scope_policy.ManualScopeError(
                f"{underlying} is not active and tradable at Alpaca."
            )
        return asset

    def _validate_manual_broker_contract(self, parsed):
        """Validate one exact manual contract through Alpaca before submission."""
        self._validate_manual_equity_underlying(parsed["underlying"])
        try:
            contract = self.trading.get_option_contract(parsed["symbol"])
        except Exception as exc:
            raise manual_scope_policy.ManualScopeError(
                f"Alpaca did not find option contract {parsed['symbol']}: {exc}"
            ) from exc
        symbol = str(self._model_field(contract, "symbol", "")).upper()
        underlying = str(self._model_field(contract, "underlying_symbol", "")).upper()
        expiry = self._model_field(contract, "expiration_date")
        right = self._enum_text(self._model_field(contract, "type"))
        expected_right = "call" if parsed["right"] == "C" else "put"
        status = self._enum_text(self._model_field(contract, "status"))
        tradable = self._model_field(contract, "tradable", False)
        if (symbol != parsed["symbol"] or underlying != parsed["underlying"]
                or expiry != parsed["expiry"] or right != expected_right):
            raise manual_scope_policy.ManualScopeError(
                f"Alpaca contract metadata did not match {parsed['symbol']}; order blocked."
            )
        if status != self._enum_text(AssetStatus.ACTIVE) or tradable is not True:
            raise manual_scope_policy.ManualScopeError(
                f"{parsed['symbol']} is not active and tradable at Alpaca."
            )
        return contract

    def open_trade(self, cfg, *, manual=False):
        request_id = str(cfg.pop("request_id", "") or uuid.uuid4())
        sym = cfg["symbol"].upper().strip()
        cfg["symbol"] = sym
        is_option = cfg.get("type", "stock") == "option"

        # A Scalpr Guard is an execution engine, not a passive alert. Refuse to
        # create one unless this specific request explicitly authorizes the
        # whole-position automatic exit. This prevents older dashboard tabs or
        # API clients from silently opting a position into broker-side sells.
        if cfg.get("auto_exit_authorized") is not True:
            self.health.record("execution", "ORDER_BLOCKED",
                               detail="automatic exit was not explicitly authorized",
                               fields={"symbol": sym, "side": "buy",
                                       "initiated_by": "dashboard_new_trade",
                                       "request_id": request_id}, force=True)
            raise HTTPException(
                409, "Automatic whole-position exits were not authorized. "
                     "Acknowledge the auto-exit control before engaging Scalpr.")

        if manual:
            if self.live:
                raise HTTPException(422, "Expanded manual trades are physically blocked in live mode.")
            try:
                scope_record = manual_scope_policy.validate_manual_trade(
                    sym, cfg.get("type", "stock"))
                self._validate_manual_broker_contract(scope_record)
            except manual_scope_policy.ManualScopeError as e:
                raise HTTPException(422, str(e))
            cfg["scope_class"] = scope_record["scope_class"]
            cfg["scope_version"] = manual_scope_policy.SCOPE_VERSION
            cfg["entry_source"] = "manual_builder"
        else:
            try:
                scope_policy.validate_trade(sym, cfg.get("type", "stock"))
            except scope_policy.ScopeError as e:
                raise HTTPException(422, str(e))
            cfg["scope_class"] = manual_scope_policy.SCOPE_VALIDATED
            cfg["scope_version"] = scope_policy.SCOPE_VERSION
            cfg["entry_source"] = cfg.get("entry_source", "automated_or_legacy")
        climb_raw = cfg.get("climb_adds", {})
        if climb_raw.get("enabled"):
            if cfg["scope_class"] != manual_scope_policy.SCOPE_VALIDATED:
                raise HTTPException(
                    422, "Automated climb adds remain limited to validated SPY 0-2 DTE options.")
            if self.live:
                raise HTTPException(422, "Climb adds are physically blocked in live mode.")
            try:
                import climb_ladder
                climb_ladder.ClimbConfig.from_dict(climb_raw, cfg.get("qty", 0))
                if not climb_ladder.feature_enabled():
                    raise HTTPException(
                        409, "Paper climb adds are not armed on this server. Start it with "
                             "SCALPR_CLIMB_ADDS_ENABLED=1 before engaging this trade.")
            except HTTPException:
                raise
            except (TypeError, ValueError) as e:
                raise HTTPException(422, f"Invalid climb-add configuration: {e}")

        # Pre-flight check 1: option symbol must be a full OCC contract, not a plain ticker
        if is_option and not self.OCC_RE.match(sym):
            raise HTTPException(422,
                f"“{sym}” isn’t a full option contract symbol. Option mode needs the OCC "
                f"code (e.g. SPY260722C00560000), not a plain ticker. Copy it from Alpaca’s "
                f"options chain, or switch Type to Stock if you meant to buy shares.")

        # Pre-flight check 2: is the market open? (only matters when placing a new buy)
        if cfg.get("buy", True):
            try:
                clock = self.trading.get_clock()
                if not clock.is_open:
                    nxt = clock.next_open.astimezone().strftime("%a %b %-d at %-I:%M %p")
                    raise HTTPException(409,
                        f"The market is closed right now — orders can’t fill. "
                        f"Next open: {nxt}. Try again then.")
            except HTTPException:
                raise
            except Exception as exc:
                # Market status is execution-critical.  A broker clock outage is
                # not permission to continue toward submit_order; fail closed and
                # make the refusal visible in the runtime health stream.
                self.health.record(
                    "execution", "ORDER_BLOCKED",
                    detail="market clock unavailable",
                    fields={"symbol": sym, "side": "buy",
                            "initiated_by": "dashboard_new_trade",
                            "request_id": request_id,
                            "error_type": type(exc).__name__},
                    force=True,
                )
                raise HTTPException(
                    503, "Couldn't verify that the market is open; no order was sent."
                ) from exc

        with LOCK:
            if sym in self.guards and not self.guards[sym].done:
                self.health.record("execution", "ORDER_BLOCKED", detail="already guarded",
                                   fields={"symbol": sym, "side": "buy",
                                           "initiated_by": "dashboard_new_trade",
                                           "request_id": request_id}, force=True)
                raise HTTPException(
                    409, f"{sym} is already guarded. Use the explicit Add contracts "
                         "control on its Guard card; no duplicate order was sent.")
        if cfg.get("buy", True):
            req = (MarketOrderRequest(symbol=sym, qty=cfg["qty"], side=OrderSide.BUY,
                                      time_in_force=TimeInForce.DAY)
                   if is_option else
                   MarketOrderRequest(symbol=sym, notional=cfg["notional"],
                                      side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
            try:
                self.health.record("execution", "ORDER_REQUESTED", fields={
                    "symbol": sym, "side": "buy", "qty": cfg.get("qty"),
                    "initiated_by": "dashboard_new_trade", "request_id": request_id,
                }, force=True)
                order = self.trading.submit_order(req)
            except Exception as e:
                # surface the BROKER'S actual rejection reason instead of a bare 500
                log(f"{sym}: order REJECTED by broker — {e}")
                self.health.record("execution", "ORDER_REJECTED", detail=str(e)[:200],
                                   fields={"symbol": sym, "side": "buy",
                                           "initiated_by": "dashboard_new_trade",
                                           "request_id": request_id}, force=True)
                raise HTTPException(502,
                    f"Broker rejected the order for {sym}: {e} — common causes: options "
                    f"trading not enabled/approved on this Alpaca paper account; insufficient "
                    f"buying power; or the contract isn’t currently tradable (e.g. a 0DTE "
                    f"contract too close to expiration).")
            entry, qty = self._wait_fill(order.id)
            log(f"{sym}: filled {qty} @ {entry}")
            self.health.record("execution", "FILLED",
                               fields={"symbol": sym, "side": "buy", "qty": qty, "price": entry,
                                       "initiated_by": "dashboard_new_trade",
                                       "request_id": request_id},
                               force=True)
        else:
            try:
                pos = self.trading.get_open_position(sym)
            except Exception as e:
                raise HTTPException(404,
                    f"Couldn’t find an open {sym} position to guard (or the broker call "
                    f"failed): {e}")
            entry, qty = float(pos.avg_entry_price), abs(float(pos.qty))
            log(f"{sym}: guarding existing {qty} @ {entry}")
        # SAFETY ORDERING: create the Guard immediately after broker-confirmed
        # state. Optional analytics must never create a filled-but-unguarded gap.
        guard_cls = (AsymmetricHardStopGuard
                     if cfg.get("hard_stop_mode") == "asymmetric_hard_stop_v1"
                     else Guard)
        guard = guard_cls(cfg, entry, qty, None)
        with LOCK:
            self.guards[sym] = guard
        cache = getattr(self, "dashboard_cache", None)
        if cache is not None:
            cache.invalidate("holdings")
        if guard.scope_class == manual_scope_policy.SCOPE_VALIDATED:
            self._start_entry_enrichment(guard)
        else:
            self.health.record(
                "research_isolation", "EXCLUDED",
                detail="manual out-of-envelope trade excluded from entry enrichment and cohorts",
                fields={"symbol": sym, "scope_class": guard.scope_class}, force=True)
        return guard.snapshot()

    def _start_entry_enrichment(self, guard):
        self._entry_enrichment_queue.put(guard)

    def _enrich_entry_context(self, guard):
        """Best-effort analytics after protection exists; never blocks entry."""
        entry_signals = self._precheck_snapshot(guard.symbol, guard.kind)
        with LOCK:
            if self.guards.get(guard.symbol) is guard:
                guard.entry_signals = entry_signals
        # Entry-incubation shadow is research-only and may make network calls.
        try:
            import incubation_config as _icfg
            if _icfg.enabled() and guard.kind == "option":
                import incubation_server as _iserv
                guard.incubation_trade_id = _iserv.on_live_entry(self, guard.symbol, guard).get("trade_id")
        except Exception as e:
            log(f"incubation entry hook skipped: {e}")

    def _precheck_snapshot(self, symbol, trade_type):
        """Best-effort market-read snapshot for the journal. Never blocks or
        fails a trade — a data hiccup just means that row's signals are blank."""
        try:
            from precheck import run_precheck
            return run_precheck(self.stock_data, symbol, trade_type)
        except Exception as e:
            log(f"{symbol}: signal snapshot failed — {e}")
            return None

    def _wait_fill(self, order_id, timeout=30):
        for _ in range(timeout):
            o = self.trading.get_order_by_id(order_id)
            if o.status.value == "filled":
                return float(o.filled_avg_price), float(o.filled_qty)
            if o.status.value in ("canceled", "expired", "rejected"):
                raise HTTPException(502, f"Order was {o.status.value} by the broker.")
            time.sleep(1)
        # Didn't fill in time — cancel it so it doesn't fill later unguarded
        try:
            self.trading.cancel_order_by_id(order_id)
        except Exception:
            pass
        raise HTTPException(504,
            "Order didn’t fill in time and was canceled. This usually means the market "
            "is closed or the symbol isn’t currently tradable. Nothing is left open.")

    def sell(self, guard: Guard, reason: str, initiated_by="guard_ratchet", request_id=None):
        log(f"{guard.symbol}: SELL — {reason}")
        request_id = request_id or str(uuid.uuid4())
        if getattr(self, "capture_guard_events", False):
            _capture_guard_event("sell_requested", guard, actor=initiated_by,
                                 reason=reason, request_id=request_id)
        self.health.record("execution", "ORDER_REQUESTED", fields={
            "symbol": guard.symbol, "side": "sell", "qty": guard.qty,
            "reason": reason, "initiated_by": initiated_by, "request_id": request_id,
        }, force=True)
        with guard.execution_lock:
            try:
                order = self.trading.submit_order(MarketOrderRequest(
                    symbol=guard.symbol, qty=guard.qty, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY))
                exit_price, _ = self._wait_fill(order.id)
            except Exception as e:
                log(f"{guard.symbol}: SELL FAILED — {e}. Intervene manually.")
                self.health.record("execution", "SELL_FAILED", detail=str(e)[:200],
                                   fields={"symbol": guard.symbol, "qty": guard.qty,
                                           "initiated_by": initiated_by,
                                           "request_id": request_id}, force=True)
                guard.done = False
                return
        realized = (exit_price - guard.entry) / guard.entry * 100
        # Journal the confirmed exit immediately. Optional signal enrichment is
        # never allowed to stall the execution loop after a sell.
        self._journal(guard, exit_price, realized, reason, None)
        log(f"{guard.symbol}: banked {realized:+.2f}% (peak {guard.peak:+.2f}%)")
        self.health.record("execution", "FILLED",
                           fields={"symbol": guard.symbol, "side": "sell", "qty": guard.qty,
                                   "price": exit_price, "reason": reason,
                                   "initiated_by": initiated_by, "request_id": request_id},
                           force=True)
        cache = getattr(self, "dashboard_cache", None)
        if cache is not None:
            cache.invalidate("holdings")
        try:
            if guard.incubation_trade_id:
                import incubation_server
                incubation_server.mark_live_exit(
                    guard.incubation_trade_id, reason, exit_price,
                    initiated_by=initiated_by)
        except Exception as e:
            log(f"incubation exit marker skipped: {e}")

    def _journal(self, g, exit_price, realized, reason, exit_signals=None):
        _migrate_journal()
        new = not JOURNAL.exists()
        with JOURNAL.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            if new:
                w.writeheader()
            row = {
                "utc_time": datetime.now(timezone.utc).isoformat(),
                "mode": "live" if self.live else "paper",
                "symbol": g.symbol, "qty": g.qty,
                "entry": f"{g.entry:.4f}", "exit": f"{exit_price:.4f}",
                "peak_pct": f"{g.peak:.3f}", "realized_pct": f"{realized:.3f}",
                "reason": reason,
                "scope_class": g.scope_class,
                "scope_version": g.scope_version,
                "entry_source": g.entry_source,
            }
            for k, v in _flatten_signals(g.entry_signals).items():
                row[f"entry_{k}"] = v
            for k, v in _flatten_signals(exit_signals).items():
                row[f"exit_{k}"] = v
            w.writerow(row)
        try:
            if self.store is not None:
                self.store.append_trade(row)
            self.dashboard_cache.invalidate("journal", "stats:paper", "stats:live")
        except Exception as e:
            log(f"journal SQLite mirror skipped: {e}")

    # ── loops ──
    def _poll_loop(self):
        while True:
            cycle_started = time.time()
            prior_cycle = self._last_guard_cycle_at
            self._last_guard_cycle_at = cycle_started
            if prior_cycle is not None and cycle_started - prior_cycle > max(3.0, POLL_SECONDS * 3):
                self.health.record("guard_loop", "LATE",
                                   detail=f"cycle gap {cycle_started - prior_cycle:.2f}s",
                                   fields={"cycle_gap_seconds": round(cycle_started - prior_cycle, 3)},
                                   force=True)
            try:
                with LOCK:
                    stocks = [g.symbol for g in self.guards.values()
                              if not g.done and g.kind == "stock"]
                    options = [g.symbol for g in self.guards.values()
                               if not g.done and g.kind == "option"]
                prices = {}
                unavailable = {}
                if stocks:
                    feed_enum = DataFeed.SIP if self.feed == "sip" else DataFeed.IEX
                    res = self.stock_data.get_stock_latest_quote(
                        StockLatestQuoteRequest(symbol_or_symbols=stocks, feed=feed_enum))
                    for s, q in res.items():
                        # Guard off the executable bid. Missing bids fail closed;
                        # an ask cannot be sold into and must never set a peak.
                        px = float(q.bid_price or 0)
                        if px:
                            prices[s] = px
                        else:
                            unavailable[s] = ("NO_BID", "stock quote has no executable bid")
                if options:
                    res = self.option_data.get_option_latest_quote(
                        OptionLatestQuoteRequest(symbol_or_symbols=options))
                    for s, q in res.items():
                        # Frozen evidence contract: option Guard materiality and
                        # exits use executable bid only. Never mid, ask, or last.
                        px = float(q.bid_price or 0)
                        if px:
                            prices[s] = px
                        else:
                            unavailable[s] = ("NO_BID", "option quote has no executable bid")
                for sym in stocks + options:
                    if sym not in prices and sym not in unavailable:
                        unavailable[sym] = ("QUOTE_MISSING", "provider returned no quote")
                fired = []
                with LOCK:
                    for sym, (status, detail) in unavailable.items():
                        g = self.guards.get(sym)
                        if g and not g.done:
                            reason = g.mark_quote_unavailable(status, detail)
                            if reason:
                                g.done = True
                                fired.append((g, reason))
                    for sym, price in prices.items():
                        g = self.guards.get(sym)
                        if g and not g.done:
                            reason = g.on_price(price)
                            if reason:
                                g.done = True
                                fired.append((g, reason))
                for g, reason in fired:
                    self.sell(g, reason, initiated_by="guard_ratchet")
                self.feed_error = None
                if unavailable:
                    self.health.record("guard_quotes", "DEGRADED", detail="missing executable bid",
                                       fields={"symbols": sorted(unavailable)})
                else:
                    self.health.record("guard_quotes", "OK")
                self._maybe_reconcile_guards()
                self.health.record("guard_loop", "OK",
                                   fields={"cycle_ms": round((time.time() - cycle_started) * 1000, 1)})
            except Exception as e:
                msg = str(e)
                if self.feed == "sip" and ("subscription" in msg.lower() or "403" in msg):
                    self.feed_error = ("SIP feed selected but your Alpaca plan doesn't "
                                       "include it. Upgrade to Algo Trader Plus, or restart "
                                       "without --sip to use the free IEX feed.")
                    log("SIP not permitted on this account — " + self.feed_error)
                else:
                    log(f"poll skipped: {e}")
                self.health.record("guard_poll", "ERROR", detail=str(e)[:200])
            time.sleep(POLL_SECONDS)

    def _automation_loop(self):
        """Paper automation is isolated so it can never delay Guard exits."""
        while True:
            try:
                self._climb_add_tick()
            except Exception as e:
                log(f"climb add tick skipped: {e}")
            time.sleep(2.0)

    def _research_loop(self):
        """Bounded shadow work, physically separate from the execution loop."""
        next_wave = next_incubation = 0.0
        while True:
            now = time.time()
            try:
                guard = self._entry_enrichment_queue.get_nowait()
            except queue.Empty:
                guard = None
            if guard is not None:
                try:
                    self._enrich_entry_context(guard)
                finally:
                    self._entry_enrichment_queue.task_done()
                time.sleep(1.0)
                continue
            if now >= next_wave:
                try:
                    import wave_server
                    wave_server.observer_tick(self)
                except Exception as e:
                    log(f"wave observer tick skipped: {e}")
                next_wave = time.time() + 10.0
            if now >= next_incubation:
                try:
                    import incubation_server
                    incubation_server.observer_tick(self, max_trades=1)
                except Exception as e:
                    log(f"incubation observer tick skipped: {e}")
                next_incubation = time.time() + 5.0
            time.sleep(0.5)

    def _provider_capture_loop(self):
        """Bounded vendor capture, isolated from Guard and order execution."""
        flow_enabled = os.getenv("SCALPR_UW_INGESTION_ENABLED", "") == "1"
        ivol_enabled = os.getenv("SCALPR_IVOL_CAPTURE_ENABLED", "") == "1"
        try:
            from unusual_whales_adapter import UnusualWhalesAdapter
            from institutional_flow_store import InstitutionalFlowStore
            self.flow_adapter = UnusualWhalesAdapter.from_environment()
            self.flow_store = InstitutionalFlowStore()
            self.institutional_flow_ingestion_running = bool(
                flow_enabled and self.flow_adapter.status().get("configured"))
        except Exception as exc:
            self.health.record("institutional_flow_capture", "DISABLED",
                               detail=f"initialization failed: {str(exc)[:140]}", force=True)
        try:
            from ivolatility_adapter import IVolatilityClient
            from options_feature_store import OptionsFeatureStore
            self.options_client = IVolatilityClient.from_environment()
            self.options_store = OptionsFeatureStore()
            self.options_capture_running = bool(
                ivol_enabled and self.options_client.status().get("configured"))
        except Exception as exc:
            self.health.record("options_capture", "DISABLED",
                               detail=f"initialization failed: {str(exc)[:140]}", force=True)

        next_flow, next_ivol = 0.0, 0.0
        ivol_completed_dates = set()
        while True:
            schedule = _provider_capture_window()
            self.institutional_flow_market_window = schedule["flow_active"]
            now = time.time()
            if (self.institutional_flow_ingestion_running
                    and schedule["flow_active"] and now >= next_flow):
                try:
                    import asyncio
                    from institutional_flow_ingestion import InstitutionalFlowIngestionService
                    service = InstitutionalFlowIngestionService(
                        self.flow_adapter, self.flow_store)
                    result = asyncio.run(service.run_once(
                        ticker="SPY", lookback_minutes=5))
                    self.institutional_flow_last_result = {
                        **result, "captured_at": datetime.now(timezone.utc).isoformat()}
                    status = "OK" if result.get("provider_status") == "AVAILABLE" else "DEGRADED"
                    self.health.record("institutional_flow_capture", status,
                                       detail=None if status == "OK" else result.get("error"),
                                       fields={k: result.get(k) for k in (
                                           "events_received", "events_persisted", "snapshots")})
                except Exception as exc:
                    self.institutional_flow_last_result = {
                        "provider_status": "UNAVAILABLE", "error": type(exc).__name__,
                        "captured_at": datetime.now(timezone.utc).isoformat()}
                    self.health.record("institutional_flow_capture", "ERROR",
                                       detail=str(exc)[:180])
                next_flow = time.time() + 60.0

            market_date = schedule["market_date"]
            if (self.options_capture_running and schedule["ivol_eod_due"]
                    and market_date not in ivol_completed_dates and now >= next_ivol):
                try:
                    from options_capture_service import OptionsCaptureService
                    service = OptionsCaptureService(self.options_client, self.options_store)
                    result = service.capture_eod(symbol="SPY", trade_date=market_date)
                    self.options_capture_last_result = {
                        **result, "trade_date": market_date,
                        "captured_at": datetime.now(timezone.utc).isoformat()}
                    source_status = result.get("source_status")
                    if source_status == "pending":
                        next_ivol = time.time() + 900.0
                        status = "PENDING"
                    else:
                        ivol_completed_dates.add(market_date)
                        status = "OK" if result.get("promoted") else "DEGRADED"
                    self.health.record("options_capture", status,
                                       detail=result.get("reason"), fields={
                                           "trade_date": market_date,
                                           "source_status": source_status,
                                           "promoted": result.get("promoted"),
                                           "contract_count": result.get("contract_count", 0),
                                       })
                except Exception as exc:
                    self.options_capture_last_result = {
                        "source_status": "error", "error": type(exc).__name__,
                        "trade_date": market_date,
                        "captured_at": datetime.now(timezone.utc).isoformat()}
                    self.health.record("options_capture", "ERROR", detail=str(exc)[:180])
                    next_ivol = time.time() + 900.0
            time.sleep(5.0)

    def _entry_bid_collector_loop(self):
        """Default-off read-only bid evidence worker; no Guard/order calls."""
        try:
            import entry_bid_collector_v1 as collector_module
            import scalpr_config
            if not scalpr_config.entry_bid_capture_enabled():
                self.entry_bid_collector_status = collector_module.disabled_status()
                self.health.record(
                    "entry_bid_collector", "DISABLED",
                    detail="ENTRY_INTEL_BID_CAPTURE_ENABLED is not 1", force=True)
                return
            if self.live:
                self.entry_bid_collector_status = {
                    **collector_module.disabled_status(),
                    "state": "BLOCKED_LIVE_MODE",
                }
                self.health.record(
                    "entry_bid_collector", "BLOCKED",
                    detail="collector is paper/shadow-only", force=True)
                return
            self.entry_bid_collector = collector_module.EntryBidCollector(
                stock_data=self.stock_data, option_data=self.option_data, feed=self.feed)
            prior_state = None
            while True:
                status = self.entry_bid_collector.tick(
                    market_active=_regular_session_active(),
                    session_open_et=self.session_open_et,
                    session_close_et=self.session_close_et)
                self.entry_bid_collector_status = status
                state = status.get("state")
                if state != prior_state:
                    self.health.record(
                        "entry_bid_collector",
                        "OK" if state in {"ACTIVE_RTH_CAPTURE", "ARMED_MARKET_CLOSED"}
                        else "DEGRADED",
                        detail=status.get("detail"),
                        fields={
                            "state": state,
                            "market_window": status.get("market_window"),
                            "active_decisions": status.get("active_decisions"),
                            "formal_cohort_eligible": False,
                        }, force=True)
                    log(f"entry bid collector: {state}")
                    prior_state = state
                time.sleep(5.0)
        except Exception as exc:
            self.entry_bid_collector_status = {
                "enabled": False, "state": "BLOCKED_INITIALIZATION",
                "detail": f"{type(exc).__name__}: {str(exc)[:180]}",
                "execution_authority": False, "guard_access": False,
                "formal_cohort_eligible": False,
            }
            self.health.record(
                "entry_bid_collector", "BLOCKED",
                detail=self.entry_bid_collector_status["detail"], force=True)

    def _climb_add_tick(self):
        """Throttled paper-only bridge from Wave gates to reconciled adds."""
        import climb_ladder
        if self.live or not climb_ladder.feature_enabled():
            return
        now = time.time()
        with LOCK:
            guards = [g for g in self.guards.values() if not g.done and not g.paused
                      and g.kind == "option" and g.cfg.get("climb_adds", {}).get("enabled")
                      and now >= g.climb_next_poll]
            for guard in guards:
                poll = float(guard.cfg.get("climb_adds", {}).get("poll_seconds", 5.0))
                guard.climb_next_poll = now + max(2.0, poll)
        for guard in guards:
            try:
                parsed = scope_policy.validate_option(guard.symbol)
                side = "CALL" if parsed["right"] == "C" else "PUT"
                initial_qty = (guard.qty if guard.climb is None
                               else guard.climb["position"]["initial_quantity"])
                cfg = climb_ladder.ClimbConfig.from_dict(guard.cfg.get("climb_adds"), initial_qty)
                import wave_server
                seq = 1 if guard.climb is None else guard.climb["position"]["wave_sequence"] + 1
                obs = wave_server.make_observation(self, {
                    "underlying_symbol": parsed["underlying"],
                    "contract_symbol": guard.symbol, "direction": side,
                }, seq, climb_ladder.wave_config(cfg, initial_qty))
                if guard.climb is None:
                    guard.climb = climb_ladder.initialize(
                        guard.symbol, guard.entry, guard.qty, obs, guard.cfg.get("climb_adds"))
                    self._audit_climb(guard, obs, None)
                    continue
                guard.climb, intent = climb_ladder.evaluate(guard.climb, obs)
                self._audit_climb(guard, obs, intent)
                if intent:
                    self._execute_climb_add(guard, intent, obs)
            except Exception as e:
                if guard.climb:
                    guard.climb = climb_ladder.reject(guard.climb, "OBSERVATION_OR_POLICY_ERROR")
                self.health.record("climb_add", "ERROR", detail=str(e)[:200],
                                   fields={"symbol": guard.symbol})

    def _execute_climb_add(self, guard, intent, obs):
        """Submit one price-capped PAPER add and trust broker-confirmed state."""
        import climb_ladder
        if self.live:
            guard.climb = climb_ladder.reject(guard.climb, "LIVE_MODE_HARD_BLOCK")
            return
        with guard.execution_lock:
            before = None
            try:
                before = self.trading.get_open_position(guard.symbol)
                if abs(float(before.qty) - float(guard.qty)) > 1e-9:
                    raise RuntimeError("broker quantity differs from Guard; add blocked")
                order = self.trading.submit_order(LimitOrderRequest(
                    symbol=guard.symbol, qty=int(intent["qty"]), side=OrderSide.BUY,
                    limit_price=round(float(intent["limit_price"]), 2),
                    time_in_force=TimeInForce.DAY))
                fill_price, _ = self._wait_fill(order.id, timeout=3)
                after = self.trading.get_open_position(guard.symbol)
                expected = float(before.qty) + int(intent["qty"])
                if abs(float(after.qty) - expected) > 1e-9:
                    raise RuntimeError("add fill did not reconcile to expected broker quantity")
                guard.rebase_after_add(float(after.avg_entry_price), abs(float(after.qty)))
                guard.climb = climb_ladder.apply_fill(
                    guard.climb,
                    {"price": fill_price, "qty": int(intent["qty"]), "ts": obs["ts"]}, obs)
                self.health.record("climb_add", "FILLED", fields={
                    "symbol": guard.symbol, "qty": intent["qty"], "price": fill_price,
                    "idempotency_key": intent["idempotency_key"],
                }, force=True)
            except Exception as e:
                recovered = False
                # A DAY limit can fill at the cancel boundary. Re-read Alpaca so
                # the Guard never protects a stale, smaller quantity.
                if before is not None:
                    try:
                        after = self.trading.get_open_position(guard.symbol)
                        delta = abs(float(after.qty)) - abs(float(before.qty))
                        if delta > 0:
                            before_cost = abs(float(before.qty)) * float(before.avg_entry_price)
                            after_cost = abs(float(after.qty)) * float(after.avg_entry_price)
                            recovered_price = max(0.0, (after_cost - before_cost) / delta)
                            guard.rebase_after_add(float(after.avg_entry_price), abs(float(after.qty)))
                            guard.climb = climb_ladder.apply_fill(
                                guard.climb,
                                {"price": recovered_price, "qty": int(delta), "ts": obs["ts"]}, obs)
                            guard.climb["reason_codes"] = ["LATE_FILL_RECOVERED_FROM_BROKER"]
                            recovered = True
                    except Exception:
                        pass
                if not recovered:
                    guard.climb = climb_ladder.reject(
                        guard.climb, "BROKER_ADD_REJECTED_OR_UNRECONCILED")
                self.health.record("climb_add", "REJECTED", detail=str(e)[:200],
                                   fields={"symbol": guard.symbol, "late_fill_recovered": recovered},
                                   force=True)

    def _audit_climb(self, guard, obs, intent):
        import json
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "version": guard.climb.get("version"), "symbol": guard.symbol,
            "status": guard.climb.get("status"),
            "reason_codes": guard.climb.get("reason_codes", []),
            "metrics": guard.climb.get("metrics", {}),
            "observation_ts": obs.get("ts"),
            "idempotency_key": intent.get("idempotency_key") if intent else None,
        }
        with Path("climb_add_events_v0.jsonl").open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def _tick_log_loop(self):
        """Appends one NBBO snapshot per symbol in TICK_SYMBOLS every
        TICK_SECONDS, whether or not anything is currently guarded. Runs
        forever; a bad quote or a closed market just means a skipped tick,
        never a crash."""
        import micro_read as _micro_read
        _migrate_tick_log()
        new = not TICK_LOG.exists()
        with TICK_LOG.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TICK_FIELDS)
            if new:
                w.writeheader()
        last_rotation_check = 0.0
        while True:
            try:
                if time.time() - last_rotation_check >= TICK_ROTATE_CHECK_SECONDS:
                    rotated = rotate_csv(TICK_LOG, max_bytes=TICK_ROTATE_BYTES)
                    last_rotation_check = time.time()
                    if rotated:
                        log(f"tick log rotated losslessly to {rotated['archive']}")
                        self.health.record("tick_storage", "ROTATED", fields=rotated, force=True)
                feed_enum = DataFeed.SIP if self.feed == "sip" else DataFeed.IEX
                res = self.stock_data.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=TICK_SYMBOLS, feed=feed_enum))
                received_at = datetime.now(timezone.utc)
                rows, rejected = [], {}
                for sym, q in res.items():
                    row, reason, age = _tick_quote_row(
                        sym, q, received_at=received_at)
                    if row is not None:
                        rows.append(row)
                    else:
                        rejected[sym] = {"reason": reason, "age_seconds": age}
                if rows:
                    with TICK_LOG.open("a", newline="") as f:
                        w = csv.DictWriter(f, fieldnames=TICK_FIELDS)
                        w.writerows(rows)
                    # Keep the dashboard's two-minute read in memory. The CSV is
                    # still the append-only research source of truth.
                    _micro_read.record_ticks(rows)
                    self.health.record("tick_feed", "OK", fields={
                        "symbols": sorted(r["symbol"] for r in rows),
                        "rejected": rejected,
                    })
                elif rejected:
                    reasons = {item["reason"] for item in rejected.values()}
                    status = "STALE" if reasons == {"STALE_PROVIDER_TIMESTAMP"} else "DEGRADED"
                    self.health.record(
                        "tick_feed", status,
                        detail=("latest provider quote is stale; capture skipped"
                                if status == "STALE" else
                                "provider returned no timestamp-audited usable quote"),
                        fields={"quotes": rejected})
                else:
                    self.health.record(
                        "tick_feed", "DEGRADED",
                        detail="provider returned no quotes", fields={})
            except Exception as e:
                log(f"tick log skipped: {e}")
                self.health.record("tick_feed", "ERROR", detail=str(e)[:200])
            time.sleep(TICK_SECONDS)

    def _maybe_reconcile_guards(self, interval_seconds=15.0):
        """Retire only broker-confirmed orphan guards; failures leave guards intact."""
        now = time.time()
        if now - self._last_reconcile < interval_seconds:
            return []
        self._last_reconcile = now
        try:
            open_symbols = {str(p.symbol).upper() for p in self.trading.get_all_positions()}
        except Exception as e:
            self.health.record("guard_reconciliation", "ERROR", detail=str(e)[:200])
            return []
        self._last_open_symbols = set(open_symbols)
        retired = []
        with LOCK:
            for symbol, guard in self.guards.items():
                if not guard.done and symbol not in open_symbols:
                    guard.done = True
                    retired.append(symbol)
        for symbol in retired:
            log(f"{symbol}: orphan guard retired after broker confirmed no open position")
        self.health.record("guard_reconciliation", "OK",
                           fields={"retired": retired, "open_position_count": len(open_symbols)},
                           force=bool(retired))
        return retired

    def _snapshot_loop(self):
        """Once per trading day, ~15 min after the session close, write an
        immutable observational snapshot. Idempotent (the writer keys on
        symbol+date+model_version and takes a cross-process lock), so a restart
        or double-fire won't duplicate. Skips non-trading days.

        Calendar handling is CONSERVATIVE: if the market calendar can't be
        verified we do NOT silently assume a 16:00 close (wrong on early-close
        days). We still write the record for data review, but mark it
        calendar_unverified / headline_verdict_allowed=false so it can't feed
        the research gate. Failures are logged loudly and each (symbol, date)
        is retried only a bounded number of times, never forever."""
        from datetime import time as dtime
        try:
            from zoneinfo import ZoneInfo
            ET = ZoneInfo("America/New_York")
        except Exception:
            ET = timezone.utc
        SNAPSHOT_DELAY_MIN = 15
        MAX_ATTEMPTS_PER_KEY = 5
        attempts = {}   # (sym, date) -> failed attempt count
        while True:
            try:
                now_et = datetime.now(ET)
                today = now_et.date()
                close_et, calendar_verified = None, True
                try:
                    from alpaca.trading.requests import GetCalendarRequest
                    cal = self.trading.get_calendar(GetCalendarRequest(start=today, end=today))
                    if cal:
                        ct = getattr(cal[0], "close", None)
                        # Alpaca may return close as a datetime.time OR a full
                        # datetime depending on version — handle both.
                        if isinstance(ct, datetime):
                            close_et = ct if ct.tzinfo else ct.replace(tzinfo=ET)
                        elif isinstance(ct, dtime):
                            close_et = datetime.combine(today, ct, tzinfo=ET)
                        else:
                            close_et = None
                    else:
                        close_et = None            # confirmed no session today
                except Exception as e:
                    # calendar unavailable — provisional 16:00 cutoff, but flagged
                    close_et = datetime.combine(today, dtime(16, 0), tzinfo=ET)
                    calendar_verified = False
                    log(f"snapshot: market calendar unverified ({e}); using provisional "
                        f"16:00 ET cutoff, headline verdict withheld for {today}")

                if close_et is not None and now_et.timestamp() >= close_et.timestamp() + SNAPSHOT_DELAY_MIN * 60:
                    for sym in TICK_SYMBOLS:
                        key = (sym, str(today))
                        if attempts.get(key, 0) >= MAX_ATTEMPTS_PER_KEY:
                            continue   # gave up on this key — already logged loudly
                        try:
                            import session_snapshot
                            res = session_snapshot.take_snapshot(
                                sym, market_date=str(today), session_close=close_et,
                                cutoff=close_et, calendar_verified=calendar_verified)
                            if res.get("created"):
                                log(f"session snapshot written: {sym} {today} "
                                    f"[{res.get('snapshot_status')}] → {res.get('verdict')}")
                            attempts.pop(key, None)
                        except Exception as e:
                            attempts[key] = attempts.get(key, 0) + 1
                            level = "GIVING UP" if attempts[key] >= MAX_ATTEMPTS_PER_KEY else "will retry"
                            log(f"*** SNAPSHOT FAILED ({level}) {sym} {today} "
                                f"attempt {attempts[key]}/{MAX_ATTEMPTS_PER_KEY}: {e}")
            except Exception as e:
                log(f"*** snapshot loop error: {e}")
            time.sleep(600)   # check every 10 min

    def _shadow_loop(self):
        """Premarket shadow forward-test driver. Before the open (by 09:25 ET)
        it freezes the scorecard assessment; after the close it attaches the
        session's outcomes. Both are immutable and idempotent. Best-effort:
        a data hiccup skips a day, never crashes. Observational only."""
        from datetime import time as dtime, timedelta as td
        try:
            from zoneinfo import ZoneInfo
            ET = ZoneInfo("America/New_York")
        except Exception:
            ET = timezone.utc
        while True:
            try:
                now_et = datetime.now(ET)
                today = now_et.date()
                open_et = close_et = None
                try:
                    from alpaca.trading.requests import GetCalendarRequest
                    cal = self.trading.get_calendar(GetCalendarRequest(start=today, end=today))
                    if cal:
                        ot, ct = getattr(cal[0], "open", None), getattr(cal[0], "close", None)
                        open_et = ot if isinstance(ot, datetime) else (
                            datetime.combine(today, ot, tzinfo=ET) if ot else None)
                        close_et = ct if isinstance(ct, datetime) else (
                            datetime.combine(today, ct, tzinfo=ET) if ct else None)
                        if open_et and open_et.tzinfo is None:
                            open_et = open_et.replace(tzinfo=ET)
                        if close_et and close_et.tzinfo is None:
                            close_et = close_et.replace(tzinfo=ET)
                except Exception:
                    open_et = datetime.combine(today, dtime(9, 30), tzinfo=ET)
                    close_et = datetime.combine(today, dtime(16, 0), tzinfo=ET)

                self.session_market_date = today
                self.session_open_et = open_et
                self.session_close_et = close_et

                if open_et and close_et:
                    import premarket as pm, premarket_shadow as ps
                    d = ps._session_dir("SPY", today)
                    cutoff = datetime.combine(today, ps.ASSESSMENT_CUTOFF_ET, tzinfo=ET)
                    # (a) freeze the assessment in the window before the cutoff
                    if (open_et - td(minutes=25) <= now_et <= cutoff
                            and not (d / "assessment.json").exists()):
                        try:
                            sc = pm.run_premarket(self.stock_data, "SPY")
                            res = ps.log_assessment(sc, market_date=str(today))
                            log(f"premarket shadow assessment frozen: SPY {today} "
                                f"(eligible={res.get('eligible')})")
                        except Exception as e:
                            log(f"shadow assessment failed: {e}")
                    # (b) attach premarket-shadow outcomes after the close
                    if (now_et >= close_et + td(minutes=15)
                            and (d / "assessment.json").exists()
                            and not (d / "outcomes.json").exists()):
                        try:
                            bars, pdh, pdl = self._shadow_session_bars("SPY", open_et, close_et)
                            res = ps.attach_outcomes("SPY", str(today), bars, pdh, pdl)
                            if res.get("attached"):
                                log(f"premarket shadow outcomes attached: SPY {today}")
                        except Exception as e:
                            log(f"shadow outcomes failed: {e}")

                    # (c) EXPLORATORY entry-policy: sample a decision each pass while
                    # the market is open, and attach hypothetical outcomes after close.
                    import entry_policy as _ep
                    if open_et <= now_et < close_et:
                        try:
                            _entry_read = _ep.assess_entry(
                                self.stock_data, "SPY", feed=self.feed)
                            _ep.log_decision(_entry_read, str(today))
                            import directional_shadow as _ds
                            import wave_server as _ws
                            _contracts = _ws.list_contracts("SPY", limit=None).get("contracts", [])
                            _ds.append_proposal(_ds.build_proposal(_entry_read, _contracts))
                        except Exception as e:
                            log(f"entry-policy sample failed: {e}")
                        # (c2) Scalpr Intelligence: freeze the decision-time feature
                        # snapshot + in-band contract universe (immutable; the close
                        # labeler reads it and never regenerates it). Idempotent/min.
                        try:
                            import feature_engine as _fe
                            from workup_api import run_path as _run_path
                            _p = _run_path("SPY")
                            _payload = json.loads(_p.read_text()) if _p.exists() else None
                            _fr = _fe.build_feature_record(self.stock_data, "SPY",
                                                           workup_payload=_payload)
                            _fe.persist_feature_snapshot(
                                _fr, (_payload or {}).get("contracts") or [], str(today),
                                quote_as_of=(_payload or {}).get("as_of"))
                        except Exception as e:
                            log(f"intel snapshot failed: {e}")
                    if now_et >= close_et + td(minutes=15):
                        try:
                            ebars, _pdh, _pdl = self._shadow_session_bars("SPY", open_et, close_et)
                            r = _ep.attach_outcomes("SPY", str(today), ebars)
                            if r.get("attached"):
                                log(f"entry-policy outcomes attached: SPY {today} ({r['attached']})")
                        except Exception as e:
                            log(f"entry-policy outcomes failed: {e}")
                        # (c3) Scalpr Intelligence label lifecycle — SAME workflow,
                        # not a second scheduler. Advances every label it can:
                        # finalizes elapsed horizons, leaves multi-session ones
                        # PENDING for a later close. Reads frozen snapshots only.
                        try:
                            import label_lifecycle as _ll
                            s = _ll.run_lifecycle(self._intel_forward_bars)
                            log("intel label lifecycle: "
                                f"snap={s['snapshots_examined']} ctr={s['contracts_examined']} "
                                f"final={s['labels_finalized']} pending={s['labels_pending']} "
                                f"unlabelable={s['labels_unlabelable']} corr={s['corrections']} "
                                f"retries={s['retries']} errors={s['errors']}")
                        except Exception as e:
                            log(f"intel label lifecycle failed: {e}")
            except Exception as e:
                log(f"shadow loop error: {e}")
            time.sleep(120)   # tighter cadence so the pre-open window isn't missed

    def _shadow_session_bars(self, symbol, open_et, close_et):
        """Minute bars from the official open to the close, as
        {t: minutes-from-open, open, high, low, close, volume}, plus prior-day
        high/low for PDH/PDL-touch outcomes."""
        feed_enum = DataFeed.SIP if self.feed == "sip" else DataFeed.IEX
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                               start=open_et.astimezone(timezone.utc),
                               end=close_et.astimezone(timezone.utc), feed=feed_enum)
        raw = self.stock_data.get_stock_bars(req).data.get(symbol) or []
        open_ts = open_et.astimezone(timezone.utc)
        bars = []
        for b in raw:
            t = int(round((b.timestamp - open_ts).total_seconds() / 60))
            if t < 0:
                continue
            bars.append({"t": t, "open": float(b.open), "high": float(b.high),
                         "low": float(b.low), "close": float(b.close),
                         "volume": float(getattr(b, "volume", 0) or 0)})
        bars.sort(key=lambda x: x["t"])
        # prior-day high/low from daily bars (bar before today)
        pdh = pdl = None
        try:
            dreq = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                                    start=datetime.now(timezone.utc) - timedelta(days=10))
            days = self.stock_data.get_stock_bars(dreq).data.get(symbol) or []
            if len(days) >= 2:
                pdh, pdl = float(days[-2].high), float(days[-2].low)
        except Exception:
            pass
        return bars, pdh, pdl

    def _intel_forward_bars(self, ticker, decision_ts_utc, now_utc):
        """UNDERLYING minute bars STRICTLY after the decision timestamp and within
        the REAL trading session of each date, across as many sessions as have
        elapsed, as [{ts(iso), high, low, close}].

        Session bounds come from the exchange trading CALENDAR (handles early
        closes, holidays, and — via ET conversion — daylight-saving), never a
        hardcoded 09:30–16:00. Because 1 in-session minute bar ≈ 1 trading
        minute, bar-count equals elapsed trading minutes even across early
        closes and multi-session holds. Raises on a data/calendar error so the
        lifecycle records ERROR_RETRYABLE instead of assuming a standard session
        or fabricating a result."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        from alpaca.trading.requests import GetCalendarRequest
        ET = ZoneInfo("America/New_York")
        start_d = decision_ts_utc.astimezone(ET).date()
        end_d = now_utc.astimezone(ET).date()
        cal = self.trading.get_calendar(GetCalendarRequest(start=start_d, end=end_d))
        sessions = {}                       # date -> (open_et, close_et), real bounds
        for c in cal:
            o, cl = c.open, c.close
            if hasattr(o, "date"):          # datetime
                o_dt = o.astimezone(ET) if o.tzinfo else o.replace(tzinfo=ET)
                cl_dt = cl.astimezone(ET) if cl.tzinfo else cl.replace(tzinfo=ET)
            else:                           # time
                o_dt = _dt.combine(c.date, o, tzinfo=ET)
                cl_dt = _dt.combine(c.date, cl, tzinfo=ET)
            sessions[c.date] = (o_dt, cl_dt)
        feed_enum = DataFeed.SIP if self.feed == "sip" else DataFeed.IEX
        req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute,
                               start=decision_ts_utc, end=now_utc, feed=feed_enum)
        raw = self.stock_data.get_stock_bars(req).data.get(ticker) or []
        out = []
        for b in raw:
            if b.timestamp <= decision_ts_utc:
                continue
            et = b.timestamp.astimezone(ET)
            sess = sessions.get(et.date())
            if not sess:                    # holiday / non-session date
                continue
            o_dt, cl_dt = sess
            if not (o_dt <= et < cl_dt):    # real session bounds (early close aware)
                continue
            out.append({"ts": b.timestamp.isoformat(), "high": float(b.high),
                        "low": float(b.low), "close": float(b.close)})
        return out

    # ── option chain helpers (for the builder UI) ──
    def list_expirations(self, underlying, ctype):
        """Manual-builder expirations for an Alpaca-validated equity, through 60 DTE."""
        underlying = manual_scope_policy.validate_underlying(underlying)
        self._validate_manual_equity_underlying(underlying)
        ct = ContractType.CALL if ctype == "C" else ContractType.PUT
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying.upper()],
            status=AssetStatus.ACTIVE, type=ct,
            expiration_date_gte=scope_policy.market_date(),
            expiration_date_lte=(scope_policy.market_date()
                                 + timedelta(days=manual_scope_policy.MAX_DTE)),
            limit=10000)
        res = self.trading.get_option_contracts(req)
        dates = sorted({c.expiration_date.isoformat() for c in res.option_contracts
                        if getattr(c, "tradable", False)})
        return dates

    def list_strikes(self, underlying, ctype, expiry):
        """Return contracts for a given ticker/type/expiry as {strike, symbol}."""
        underlying = manual_scope_policy.validate_underlying(underlying)
        self._validate_manual_equity_underlying(underlying)
        ct = ContractType.CALL if ctype == "C" else ContractType.PUT
        try:
            exp = datetime.fromisoformat(expiry).date()
        except (TypeError, ValueError) as exc:
            raise manual_scope_policy.ManualScopeError("Expiration must be YYYY-MM-DD.") from exc
        dte = (exp - scope_policy.market_date()).days
        if not manual_scope_policy.MIN_DTE <= dte <= manual_scope_policy.MAX_DTE:
            raise manual_scope_policy.ManualScopeError(
                f"Manual paper scope allows options with 0-60 DTE; {expiry} is {dte} DTE.")
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying.upper()],
            status=AssetStatus.ACTIVE, type=ct,
            expiration_date=exp, limit=10000)
        res = self.trading.get_option_contracts(req)
        out = [{"strike": float(c.strike_price), "symbol": c.symbol}
               for c in res.option_contracts if getattr(c, "tradable", False)]
        underlying_price = None
        try:
            q = self.stock_data.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=underlying.upper()))[underlying.upper()]
            underlying_price = (float(q.bid_price) + float(q.ask_price)) / 2 or float(q.ask_price)
        except Exception:
            pass
        return {"strikes": sorted(out, key=lambda x: x["strike"]),
                "underlying_price": underlying_price}

    def quote_option(self, symbol):
        """Live bid/ask/last for one option contract."""
        parsed = manual_scope_policy.validate_manual_option(symbol)
        self._validate_manual_broker_contract(parsed)
        q = self.option_data.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
        out = {"bid": float(q.bid_price), "ask": float(q.ask_price),
               "bid_size": int(q.bid_size), "ask_size": int(q.ask_size), "last": None}
        try:
            t = self.option_data.get_option_latest_trade(
                OptionLatestTradeRequest(symbol_or_symbols=symbol))[symbol]
            out["last"] = float(t.price)
        except Exception:
            pass
        return out

    def list_holdings(self):
        """Every open position in the account, with live P/L, plus whether Scalpr guards it."""
        positions = self.trading.get_all_positions()
        with LOCK:
            guards = {s: g for s, g in self.guards.items() if not g.done}
        out = []
        for p in positions:
            qty = float(p.qty)
            g = guards.get(p.symbol)
            ladder = None
            if g:
                ladder = {
                    "peak": round(g.peak, 3),
                    "tolerance": g.tolerance(),
                    "sell_level_pct": round(g.peak - g.tolerance(), 3),
                    "armed": g.tolerance() <= 0.1 or g.peak >= 20,
                    "quote_status": g.quote_status,
                    "execution_protected": g.snapshot()["execution_protected"],
                    "scope_class": g.scope_class,
                }
            out.append({
                "symbol": p.symbol,
                "asset_class": str(getattr(p, "asset_class", "")),
                "qty": qty,
                "side": "long" if qty >= 0 else "short",
                "avg_entry": float(p.avg_entry_price),
                "current": float(p.current_price) if p.current_price else None,
                "market_value": float(p.market_value) if p.market_value else None,
                "pl_dollars": float(p.unrealized_pl) if p.unrealized_pl else 0.0,
                "pl_percent": float(p.unrealized_plpc) * 100 if p.unrealized_plpc else 0.0,
                "guarded": g is not None,
                "ladder": ladder,
            })
        return sorted(out, key=lambda x: -abs(x["market_value"] or 0))

    def account_flat_proof(self):
        """Return a direct, uncached, read-only paper-account flatness proof."""
        requested_at = datetime.now(timezone.utc)
        account = self.trading.get_account()
        positions = self.trading.get_all_positions()
        open_orders = self.trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN))
        observed_at = datetime.now(timezone.utc)
        account_id = str(getattr(account, "id", "") or "")
        if not account_id:
            raise RuntimeError("Alpaca account identity is unavailable")
        status = getattr(account, "status", None)
        status = getattr(status, "value", status)
        positions_count = len(positions)
        open_orders_count = len(open_orders)
        return {
            "schema_version": "scalpr-account-flat-proof-v1",
            "source": "alpaca_trading_api_direct_uncached",
            "requested_at_utc": requested_at.isoformat(),
            "observed_at_utc": observed_at.isoformat(),
            "mode": "live" if self.live else "paper",
            "account_status": str(status or "UNKNOWN").upper(),
            "account_identity_sha256": hashlib.sha256(
                account_id.encode("utf-8")).hexdigest(),
            "positions_count": positions_count,
            "open_orders_count": open_orders_count,
            "flat": positions_count == 0 and open_orders_count == 0,
        }

    def market_calendar_proof(self, target_session):
        """Return a direct, uncached XNYS-session proof for a cohort lock."""
        try:
            session_date = datetime.strptime(str(target_session), "%Y-%m-%d").date()
        except (TypeError, ValueError) as exc:
            raise ValueError("target_session must be YYYY-MM-DD") from exc
        from alpaca.trading.requests import GetCalendarRequest
        from zoneinfo import ZoneInfo
        observed_at = datetime.now(timezone.utc)
        sessions = self.trading.get_calendar(
            GetCalendarRequest(start=session_date, end=session_date))
        def row_date(row):
            value = getattr(row, "date", session_date)
            if isinstance(value, datetime):
                return value.date()
            if hasattr(value, "isoformat") and not isinstance(value, str):
                return value
            return datetime.strptime(str(value), "%Y-%m-%d").date()

        matching = [row for row in sessions if row_date(row) == session_date]
        if len(matching) != 1:
            return {
                "schema_version": "scalpr-market-calendar-proof-v1",
                "source": "alpaca_trading_calendar_direct_uncached",
                "market": "XNYS",
                "target_session": session_date.isoformat(),
                "observed_at_utc": observed_at.isoformat(),
                "is_trading_day": False,
                "market_open_utc": None,
                "market_close_utc": None,
            }

        def utc_iso(value):
            from datetime import time as datetime_time
            if isinstance(value, datetime_time):
                value = datetime.combine(session_date, value,
                                         tzinfo=ZoneInfo("America/New_York"))
            if not isinstance(value, datetime):
                value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=ZoneInfo("America/New_York"))
            return value.astimezone(timezone.utc).isoformat()

        row = matching[0]
        return {
            "schema_version": "scalpr-market-calendar-proof-v1",
            "source": "alpaca_trading_calendar_direct_uncached",
            "market": "XNYS",
            "target_session": session_date.isoformat(),
            "observed_at_utc": observed_at.isoformat(),
            "is_trading_day": True,
            "market_open_utc": utc_iso(row.open),
            "market_close_utc": utc_iso(row.close),
        }

    def add_to_guard(self, symbol, qty, initiated_by="dashboard_manual_add",
                     request_id=None):
        """Explicit paper-only add; reconcile broker state before rebasing Guard."""
        symbol = str(symbol or "").upper()
        request_id = request_id or str(uuid.uuid4())
        if self.live:
            raise HTTPException(422, "Manual adds are physically blocked in live mode.")
        try:
            qty = int(qty)
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"Invalid add request: {e}")
        if qty < 1:
            raise HTTPException(422, "Add quantity must be a positive whole contract count.")
        with LOCK:
            guard = self.guards.get(symbol)
            if guard is None or guard.done:
                self.health.record("execution", "ORDER_BLOCKED", detail="not guarded",
                                   fields={"symbol": symbol, "side": "buy",
                                           "initiated_by": initiated_by,
                                           "request_id": request_id}, force=True)
                raise HTTPException(409, f"{symbol} is not actively guarded.")
            if guard.paused or not guard.snapshot()["execution_protected"]:
                self.health.record("execution", "ORDER_BLOCKED", detail="guard unprotected",
                                   fields={"symbol": symbol, "side": "buy",
                                           "initiated_by": initiated_by,
                                           "request_id": request_id}, force=True)
                raise HTTPException(409, "Cannot add while the Guard is paused or unprotected.")
            if guard.climb is not None:
                raise HTTPException(409, "Manual add is blocked while automated climb-add state is active.")
            if float(guard.qty) + qty > 500:
                raise HTTPException(422, "Manual add would exceed the 500-contract paper safety cap.")
        try:
            if guard.scope_class == manual_scope_policy.SCOPE_OUT_OF_ENVELOPE:
                parsed = manual_scope_policy.validate_manual_option(symbol)
                self._validate_manual_broker_contract(parsed)
            else:
                scope_policy.validate_option(symbol)
        except (scope_policy.ScopeError, manual_scope_policy.ManualScopeError) as e:
            raise HTTPException(422, f"Invalid add request: {e}")
        self.health.record("execution", "ORDER_REQUESTED", fields={
            "symbol": symbol, "side": "buy", "qty": qty,
            "initiated_by": initiated_by, "request_id": request_id,
        }, force=True)
        with guard.execution_lock:
            before = self.trading.get_open_position(symbol)
            if abs(abs(float(before.qty)) - float(guard.qty)) > 1e-9:
                raise HTTPException(409, "Broker quantity differs from Guard; add blocked.")
            try:
                order = self.trading.submit_order(MarketOrderRequest(
                    symbol=symbol, qty=qty, side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY))
                fill_price, _ = self._wait_fill(order.id)
                after = self.trading.get_open_position(symbol)
                expected = abs(float(before.qty)) + qty
                if abs(abs(float(after.qty)) - expected) > 1e-9:
                    raise RuntimeError("filled add did not reconcile to expected broker quantity")
                guard.rebase_after_add(float(after.avg_entry_price), abs(float(after.qty)))
            except Exception as e:
                # Fail closed but adopt any late broker fill so exits protect the
                # true quantity even when the request path reports an error.
                try:
                    after = self.trading.get_open_position(symbol)
                    if abs(float(after.qty)) > 0:
                        guard.rebase_after_add(float(after.avg_entry_price), abs(float(after.qty)))
                except Exception:
                    pass
                self.health.record("execution", "ORDER_REJECTED", detail=str(e)[:200],
                                   fields={"symbol": symbol, "side": "buy", "qty": qty,
                                           "initiated_by": initiated_by,
                                           "request_id": request_id}, force=True)
                if isinstance(e, HTTPException):
                    raise
                raise HTTPException(502, f"Add failed or could not be reconciled: {e}")
        self.health.record("execution", "FILLED", fields={
            "symbol": symbol, "side": "buy", "qty": qty, "price": fill_price,
            "total_qty": guard.qty, "initiated_by": initiated_by,
            "request_id": request_id,
        }, force=True)
        cache = getattr(self, "dashboard_cache", None)
        if cache is not None:
            cache.invalidate("holdings")
        if getattr(self, "capture_guard_events", False):
            _capture_guard_event("add_filled", guard, actor=initiated_by,
                                 reason=f"added {qty} contracts", request_id=request_id)
        return {"ok": True, "request_id": request_id, "position": guard.snapshot()}

    def liquidate(self, symbol):
        """Close one position at market. Also drops it from Scalpr's guard list."""
        symbol = symbol.upper()
        request_id = str(uuid.uuid4())
        with LOCK:
            g = self.guards.get(symbol)
            active_guard = bool(g and not g.done)
            if active_guard:
                g.done = True
        if active_guard:
            self.sell(g, "manual liquidation from holdings",
                      initiated_by="dashboard_liquidate", request_id=request_id)
            if not g.done:
                raise HTTPException(502, "Guarded-position liquidation failed; Guard re-armed.")
            return {"ok": True, "symbol": symbol, "request_id": request_id}
        self.health.record("execution", "LIQUIDATION_REQUESTED", fields={
            "symbol": symbol, "initiated_by": "dashboard_liquidate",
            "request_id": request_id}, force=True)
        self.trading.close_position(symbol)
        self.health.record("execution", "LIQUIDATION_SUBMITTED", fields={
            "symbol": symbol, "initiated_by": "dashboard_liquidate",
            "request_id": request_id}, force=True)
        log(f"{symbol}: liquidated from holdings panel.")
        cache = getattr(self, "dashboard_cache", None)
        if cache is not None:
            cache.invalidate("holdings")
        return {"ok": True, "symbol": symbol, "request_id": request_id}

    def liquidate_all(self):
        """Emergency close-all. Cancels open orders and closes every position."""
        with LOCK:
            for g in self.guards.values():
                g.done = True
        self.trading.close_all_positions(cancel_orders=True)
        log("ALL positions liquidated from holdings panel.")
        return {"ok": True}

    def _market_loop(self):
        while True:
            try:
                bars = self.stock_data.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols="SPY", timeframe=TimeFrame.Minute, limit=30)).df
                closes = bars["close"].tolist()
                if len(closes) >= 20:
                    sma = sum(closes[-20:]) / 20
                    last = closes[-1]
                    trend = ("trending up" if last > sma * 1.0005
                             else "trending down" if last < sma * 0.9995 else "flat")
                    self.market_note = (f"SPY {trend} (last {last:.2f} vs 20-min avg "
                                        f"{sma:.2f}). Advisory only — the ratchet always holds.")
            except Exception as e:
                self.market_note = f"market read unavailable: {e}"
            time.sleep(60)


# ─── API ────────────────────────────────────────────────────────────────

app = FastAPI(title="Scalpr Trade")

from workup_api import router as workup_router
app.include_router(workup_router)

platform: Platform = None

# explain-v0 is deterministic by default.  The flag only permits a future
# registered plan provider; this build registers the inert adapter, so there is
# no outbound AI call even if the flag is accidentally set.
EXPLANATION_SERVICE = ExplanationService(
    feature_enabled=os.getenv("EXPLAIN_LAYER_ENABLED", "") == "1",
    audit_path=Path(__file__).parent / "explanation_records_v0.jsonl",
)
ENTRY_INTELLIGENCE_DECISIONS = (
    Path(__file__).parent / "entry_intelligence_decisions_v1.jsonl"
)


def _cached(key, ttl_seconds, compute):
    """Use one shared result across all browser tabs; fall back before startup."""
    cache = getattr(platform, "dashboard_cache", None) if platform is not None else None
    return cache.get_or_compute(key, ttl_seconds, compute) if cache else compute()


def _latest_entry_intelligence_packet(symbol: str, *, tail_bytes: int = 2 * 1024 * 1024):
    """Bounded read of the latest immutable packet for one symbol.

    The decision log can grow for months; a dashboard read must never scan it
    end-to-end or mutate it.  A truncated first tail line is discarded.
    """
    path = ENTRY_INTELLIGENCE_DECISIONS
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - int(tail_bytes))
            handle.seek(start)
            blob = handle.read()
    except OSError:
        return None
    lines = blob.splitlines()
    if start and lines:
        lines = lines[1:]
    wanted = str(symbol).upper()
    for raw in reversed(lines):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and str(payload.get("symbol", "")).upper() == wanted:
            return payload
    return None


def _regular_session_active(now=None):
    """Use the calendar bounds cached by the shadow worker; fail closed."""
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:
        et = timezone.utc
    current = now or datetime.now(et)
    if current.tzinfo is None:
        current = current.replace(tzinfo=et)
    current = current.astimezone(et)
    if current.weekday() >= 5:
        return False
    if platform is not None and getattr(platform, "session_market_date", None) == current.date():
        session_open = getattr(platform, "session_open_et", None)
        session_close = getattr(platform, "session_close_et", None)
        if session_open is None or session_close is None:
            return False  # the calendar confirmed there is no session today
        return session_open <= current < session_close
    minute = current.hour * 60 + current.minute
    return 9 * 60 + 30 <= minute < 16 * 60


def _capture_guard_event(action, guard, actor=None, reason=None, request_id=None):
    """Best-effort JSONL + SQLite audit; never changes the Guard action."""
    try:
        import guard_events
        record = guard_events.log_event(action, guard, actor=actor, reason=reason,
                                        request_id=request_id)
        store = getattr(platform, "store", None) if platform is not None else None
        if record is not None and store is not None:
            store.record_guard_event(record)
    except Exception as e:
        log(f"guard event capture skipped: {e}")


# ── Wave Riding shadow mode (feature-flagged, READ-ONLY; no live orders) ──────
# Additive and fully isolated: these endpoints never touch Guard, Platform order
# paths, or Standard mode. When WAVE_RIDING_ENABLED is not set they short-circuit
# to {"enabled": false}. Wave Riding v0 is shadow-only; no live order path exists.
@app.get("/api/wave/status")
def wave_status():
    def compute():
        import wave_config
        if not wave_config.feature_enabled():
            return {"enabled": False, "mode": "STANDARD",
                    "note": "Wave Riding shadow mode is OFF (set WAVE_RIDING_ENABLED=1)."}
        import wave_server
        return wave_server.status_payload()
    try:
        return _cached("wave_status", 8.0, compute)
    except Exception as e:
        return {"enabled": False, "error": str(e)[:200]}


@app.get("/api/wave/report")
def wave_report_endpoint():
    try:
        import wave_config
        if not wave_config.feature_enabled():
            return {"enabled": False}
        import wave_report
        return wave_report.build_report()
    except Exception as e:
        return {"enabled": False, "error": str(e)[:200]}


@app.get("/api/flow/ranking")
def flow_ranking():
    """Read-only Options-Flow Evidence ranking (🟢/🟠) from the workup cache.
    Evidence only — not a probability, edge, or buy signal."""
    try:
        import flow_evidence
        return _cached("flow_ranking", 25.0, flow_evidence.rank_from_runs)
    except Exception as e:
        return {"error": str(e)[:200], "ranking": []}


@app.get("/api/incubation/status")
def incubation_status():
    try:
        import incubation_config
        if not incubation_config.enabled():
            return {"enabled": False}
        import incubation_server
        return {"enabled": True, **incubation_server.status_payload()}
    except Exception as e:
        return {"enabled": False, "error": str(e)[:200]}


@app.get("/api/incubation/report")
def incubation_report():
    try:
        import incubation_config
        if not incubation_config.enabled():
            return {"enabled": False}
        import incubation_server
        return incubation_server.report_payload()
    except Exception as e:
        return {"enabled": False, "error": str(e)[:200]}


@app.post("/api/incubation/validate")
def incubation_validate(body: dict):
    """Evaluate an OPERATIONAL_VALIDATION trade against the clean-pass criteria.
    On pass, records validation_passed so Cohort A counting may begin."""
    try:
        import incubation_cohort
        return incubation_cohort.validation_report(body.get("trade_id"))
    except Exception as e:
        return {"status": "OPERATIONAL_VALIDATION_FAILED", "error": str(e)[:200]}


@app.get("/api/incubation/proposal")
def incubation_proposal():
    try:
        import incubation_server
        return incubation_server.proposal_payload()
    except Exception as e:
        return {"error": str(e)[:200]}


@app.get("/api/wave/cohort")
def wave_cohort_progress():
    """Read-only Cohort A accumulation progress (frozen config)."""
    try:
        import wave_config
        if not wave_config.feature_enabled():
            return {"enabled": False}
        import wave_cohort
        return {"enabled": True, **wave_cohort.cohort_progress()}
    except Exception as e:
        return {"enabled": False, "error": str(e)[:200]}


@app.get("/api/wave/contracts/{ticker}")
def wave_contracts(ticker: str):
    """Read-only contract picker source (from the workup cache). UI prefill only."""
    try:
        import wave_config
        if not wave_config.feature_enabled():
            return {"enabled": False}
        import wave_server
        return {"enabled": True, **wave_server.list_contracts(ticker)}
    except Exception as e:
        return {"enabled": False, "error": str(e)[:200]}


@app.get("/api/wave/index-capability")
def wave_index_capability(refresh: bool = False):
    """Read-only SPX index entitlement and data-density check."""
    try:
        import wave_config
        if not wave_config.feature_enabled():
            return {"available": False, "error": "FEATURE_DISABLED"}
        import wave_server
        if refresh:
            cache = getattr(platform, "dashboard_cache", None) if platform is not None else None
            if cache:
                cache.invalidate("wave_index_capability")
        return _cached("wave_index_capability", 30.0,
                       lambda: wave_server.index_capability(platform))
    except Exception as e:
        return {"available": False, "error": str(e)[:200]}


@app.post("/api/wave/start")
def wave_start(spec: dict):
    """Start a fully-SIMULATED Wave Riding shadow position. Never submits a broker
    order. Requires the feature flag; blocked if quote/ATR/sync requirements fail."""
    try:
        import wave_config
        if not wave_config.feature_enabled():
            return {"created": False, "blocking_reason": ["FEATURE_DISABLED"]}
        if platform is None:
            return {"created": False, "blocking_reason": ["PLATFORM_NOT_READY"]}
        import wave_server
        return wave_server.start_simulation(platform, spec)
    except Exception as e:
        return {"created": False, "error": str(e)[:200]}


@app.post("/api/wave/control")
def wave_control(body: dict):
    """Shadow-only pause / resume / stop / abandon. Never touches a real position."""
    try:
        import wave_config
        if not wave_config.feature_enabled():
            return {"error": "FEATURE_DISABLED"}
        if platform is None:
            return {"error": "PLATFORM_NOT_READY"}
        import wave_server
        return wave_server.control(platform, body.get("position_id"), body.get("action"))
    except Exception as e:
        return {"error": str(e)[:200]}


@app.get("/")
def index():
    return FileResponse(DASHBOARD, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/api/state")
def state():
    with LOCK:
        positions = [g.snapshot() for g in platform.guards.values() if not g.done]
    loop_age = (round(time.time() - platform._last_guard_cycle_at, 2)
                if platform._last_guard_cycle_at is not None else None)
    unprotected = [p["symbol"] for p in positions if not p.get("execution_protected")]
    return {"mode": "live" if platform.live else "paper",
            "positions": positions, "market": platform.market_note,
            "feed": platform.feed.upper(),
            "feed_error": platform.feed_error or platform.feed_startup_notice,
            "guard_loop_age_seconds": loop_age,
            "guard_loop_healthy": loop_age is not None and loop_age <= 3.0,
            "unprotected_symbols": unprotected}


@app.post("/api/trades")
def create_trade(cfg: dict):
    for field in ("symbol", "ladder"):
        if field not in cfg:
            raise HTTPException(422, f"missing {field}")
    # This is the only endpoint allowed to opt into the expanded manual policy.
    # All direct/automated Platform.open_trade callers remain narrow by default.
    return platform.open_trade(dict(cfg), manual=True)


@app.get("/api/option/expirations")
def option_expirations(underlying: str, type: str = "C"):
    try:
        return {"expirations": platform.list_expirations(underlying, type)}
    except manual_scope_policy.ManualScopeError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(502, f"Couldn't load expirations: {e}")


@app.get("/api/option/strikes")
def option_strikes(underlying: str, type: str, expiry: str):
    try:
        return platform.list_strikes(underlying, type, expiry)
    except manual_scope_policy.ManualScopeError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(502, f"Couldn't load strikes: {e}")


@app.get("/api/option/quote")
def option_quote(symbol: str):
    try:
        return platform.quote_option(symbol)
    except manual_scope_policy.ManualScopeError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(502, f"Couldn't quote {symbol}: {e}")


@app.post("/api/positions/{symbol}/sell")
def manual_sell(symbol: str):
    request_id = str(uuid.uuid4())
    with LOCK:
        g = platform.guards.get(symbol.upper())
        if not g or g.done:
            raise HTTPException(404, "not guarded")
        g.done = True
    platform.sell(g, "manual sell from dashboard",
                  initiated_by="dashboard_manual_sell", request_id=request_id)
    if not g.done:
        raise HTTPException(502, "Sell failed; the Guard was re-armed. Review broker status.")
    return {"ok": True, "request_id": request_id}


@app.post("/api/positions/{symbol}/add")
def manual_add(symbol: str, body: dict):
    """Explicit paper-only add to an already protected Guard."""
    return platform.add_to_guard(symbol, body.get("qty"))


@app.post("/api/positions/{symbol}/pause")
def pause_guard(symbol: str):
    """Disengage the ratchet on a position WITHOUT selling. While paused the guard
    tracks price but fires no exits — the position has no downside protection."""
    with LOCK:
        g = platform.guards.get(symbol.upper())
        if not g or g.done:
            raise HTTPException(404, "not guarded")
        g.paused = True
    _capture_guard_event("pause", g, actor="dashboard_pause")
    log(f"{symbol.upper()}: guard PAUSED (no exits will fire)")
    return {"ok": True, "paused": True}


@app.post("/api/positions/{symbol}/resume")
def resume_guard(symbol: str, body: dict):
    """Re-engage the ratchet, re-armed from the current price (peak reset to
    current profit, fresh grace period). A typed symbol and explicit automatic
    sell acknowledgement are required; bare/stale requests fail closed."""
    symbol = symbol.upper()
    request_id = str(body.get("request_id") or uuid.uuid4())
    if (body.get("confirm_symbol", "").upper().strip() != symbol
            or body.get("allow_automatic_sell") is not True):
        if platform is not None:
            platform.health.record(
                "execution", "GUARD_RESUME_BLOCKED",
                detail="missing typed symbol or automatic-sell acknowledgement",
                fields={"symbol": symbol, "initiated_by": "dashboard_resume",
                        "request_id": request_id}, force=True)
        raise HTTPException(
            409, f"Re-engaging {symbol} requires typing the exact contract symbol "
                 "and explicitly authorizing automatic whole-position sells.")
    with LOCK:
        g = platform.guards.get(symbol)
        if not g or g.done:
            raise HTTPException(404, "not guarded")
        try:
            g.resume()
        except ValueError as e:
            raise HTTPException(409, str(e))
    _capture_guard_event("resume", g, actor="dashboard_resume_confirmed",
                         reason="typed symbol and automatic sell acknowledged",
                         request_id=request_id)
    platform.health.record(
        "execution", "GUARD_RESUMED",
        fields={"symbol": symbol,
                "initiated_by": "dashboard_resume_confirmed",
                "request_id": request_id}, force=True)
    log(f"{symbol}: guard RESUMED after explicit auto-sell confirmation")
    return {"ok": True, "paused": False, "request_id": request_id}


@app.get("/api/holdings")
def holdings():
    """All open positions in the Alpaca account, whether Scalpr guards them or not."""
    try:
        return _cached("holdings", 2.5,
                       lambda: {"holdings": platform.list_holdings()})
    except Exception as e:
        raise HTTPException(502, f"Couldn't load holdings: {e}")


@app.get("/api/account-flat-proof")
def account_flat_proof_endpoint():
    """Direct broker read for a short-lived lock proof; deliberately uncached."""
    try:
        return platform.account_flat_proof()
    except Exception as e:
        raise HTTPException(502, f"Couldn't verify paper-account flatness: {e}")


@app.get("/api/market-calendar-proof")
def market_calendar_proof_endpoint(target_session: str):
    """Direct broker calendar read for a short-lived cohort-lock proof."""
    try:
        return platform.market_calendar_proof(target_session)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(502, f"Couldn't verify the target trading session: {e}")


@app.get("/api/storage/health")
def storage_health_endpoint():
    """Constant-memory disk/log sizing; does not read evidence contents."""
    try:
        return _cached("storage_health", 30.0, storage_health)
    except Exception as e:
        raise HTTPException(500, f"Couldn't inspect storage health: {e}")


@app.get("/api/v2/options-intelligence/status")
def options_intelligence_status():
    """Read-only Phase 1 capability status; never starts capture or trading."""
    client = getattr(platform, "options_client", None)
    store = getattr(platform, "options_store", None)
    provider = (client.status() if client is not None
                else IVolatilityClient.from_environment().status())
    running = bool(getattr(platform, "options_capture_running", False))
    provider = {**provider, "enabled": running,
                "reason": "scheduled_eod_capture" if running else provider.get("reason")}
    return {
        "phase": "phase-1-foundation",
        "paper_shadow_only": True,
        "provider": provider,
        "store": (store or OptionsFeatureStore()).status(),
        "capture_running": running,
        "last_capture": getattr(platform, "options_capture_last_result", None),
        "execution_authority": False,
    }


@app.get("/api/v2/institutional-flow/status")
def institutional_flow_status():
    """Capability status only; does not poll UW or start ingestion."""
    adapter = getattr(platform, "flow_adapter", None)
    store = getattr(platform, "flow_store", None)
    return {
        "phase": "provider-neutral-foundation",
        "paper_shadow_only": True,
        "configured_provider": "unusual_whales",
        "provider": (adapter.status() if adapter is not None
                     else UnusualWhalesAdapter.from_environment().status()),
        "store": (store or InstitutionalFlowStore()).status(),
        "ingestion_running": bool(getattr(
            platform, "institutional_flow_ingestion_running", False)),
        "market_window_active": bool(getattr(
            platform, "institutional_flow_market_window", False)),
        "last_ingestion": getattr(platform, "institutional_flow_last_result", None),
        "execution_authority": False,
    }


@app.get("/api/v2/cloudsql/status")
def cloudsql_status():
    """Local mirror-worker status; never opens a database connection here."""
    profile = Path("v2_data/cloudsql_profile.json")
    status_path = Path("v2_data/cloudsql_mirror_status.json")
    try:
        mirror = json.loads(status_path.read_text()) if status_path.exists() else {
            "status": "NOT_STARTED"}
    except (OSError, json.JSONDecodeError):
        mirror = {"status": "UNREADABLE"}
    return {
        "configured": profile.exists(),
        "worker_running": bool(
            platform is not None
            and getattr(platform, "cloudsql_mirror_process", None) is not None
            and platform.cloudsql_mirror_process.poll() is None),
        "mirror": mirror,
        "local_files_authoritative": True,
        "execution_authority": False,
    }


@app.post("/api/holdings/{symbol}/liquidate")
def liquidate(symbol: str):
    """Close a single position at market, and stop guarding it if we were."""
    try:
        return platform.liquidate(symbol)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Couldn't liquidate {symbol}: {e}")


@app.post("/api/holdings/liquidate-all")
def liquidate_all():
    """Emergency: close every open position at market."""
    try:
        return platform.liquidate_all()
    except Exception as e:
        raise HTTPException(502, f"Couldn't liquidate all: {e}")


@app.get("/api/precheck")
def precheck(symbol: str, type: str = "stock", direction: str = None):
    from precheck import run_precheck
    return run_precheck(platform.stock_data, symbol, type, direction)


@app.get("/api/entry/assess")
def entry_assess(symbol: str = "SPY"):
    """EXPLORATORY entry read (entry-policy-exploratory-v0) — LONG/SHORT_CANDIDATE
    / WAIT / NO_TRADE with hard vetoes, trigger state, and trade geometry. NOT the
    frozen entry-policy-v1: formal_cohort_eligible is permanently false and
    formal_readiness remains NO_TRADE until qualification. Never places an order."""
    import entry_policy
    if not _regular_session_active():
        return entry_policy.market_closed_assessment(symbol, feed=platform.feed)
    return _cached(
        f"entry_assess:{symbol.upper()}:{platform.feed}", 15.0,
        lambda: entry_policy.assess_entry(
            platform.stock_data, symbol, feed=platform.feed))


@app.get("/api/entry/candidates")
def entry_candidates(symbol: str = "SPY"):
    """Recent exploratory candidates logged (non-qualifying research)."""
    import entry_policy
    return {"policy_version": entry_policy.POLICY_VERSION,
            "formal_cohort_eligible": entry_policy.FORMAL_COHORT_ELIGIBLE,
            "candidates": entry_policy.recent_candidates(symbol)}


@app.get("/api/entry/summary")
def entry_summary(symbol: str = "SPY"):
    """Descriptive exploratory summary (decisions + hypothetical outcomes).
    Non-qualifying; operational validation for the first sessions, not profitability."""
    import entry_policy
    return entry_policy.exploratory_summary(symbol)


@app.get("/api/entry-intelligence/collector")
def entry_bid_collector_status():
    """Read-only status; never starts capture and never reaches the Guard."""
    if platform is None:
        return {"enabled": False, "state": "PLATFORM_NOT_READY",
                "execution_authority": False, "guard_access": False,
                "formal_cohort_eligible": False}
    service = getattr(platform, "entry_bid_collector", None)
    if service is not None:
        return service.public_status()
    return dict(getattr(platform, "entry_bid_collector_status", {}) or {})


@app.get("/api/explanation")
def evidence_explanation(symbol: str = "SPY"):
    """Deterministic explanation of the current evidence brief.

    Read-only and non-authoritative.  The current build has no external model
    adapter, makes zero outbound AI calls, and is never read by an order, Guard,
    cohort, or decision gate.
    """
    key = str(symbol).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", key):
        raise HTTPException(422, "invalid explanation symbol")

    def safe(read):
        try:
            value = read()
            return value if isinstance(value, dict) else {"available": False}
        except Exception:
            # Error strings can contain provider or transport text.  They are
            # intentionally not passed into the brief or a future model.
            return {"available": False, "error": "component_unavailable"}

    def cached_component(cache_key: str, max_age_seconds: float):
        """Read an acceleration-layer value without initiating provider work.

        Explanation is presentation, so it must never make a cold Alpaca call or
        fit a regime model. The owning dashboard endpoint populates each value;
        until then the fact remains explicitly missing.
        """
        cache = getattr(platform, "dashboard_cache", None) if platform is not None else None
        value = cache.get(cache_key, max_age_seconds) if cache is not None else None
        return value if isinstance(value, dict) else {
            "available": False, "state": "MISSING"}

    def compute():
        packet = _latest_entry_intelligence_packet(key)
        brief = build_evidence_brief(
            symbol=key,
            entry_packet=packet,
            premarket=cached_component(
                f"premarket:{key}:{platform.feed if platform is not None else 'unknown'}", 120.0),
            microread=cached_component(f"microread:{key}", 30.0),
            regime=cached_component(f"regime:{key}", 120.0),
            flow=safe(institutional_flow_status),
            ivol=safe(options_intelligence_status),
            collector=safe(entry_bid_collector_status),
        )
        result = EXPLANATION_SERVICE.explain(
            brief, request_id=str(uuid.uuid4()))
        return {
            "brief": brief.model_dump(mode="json"),
            "raw_brief_display": safe_raw_brief(brief),
            "explanation": result.model_dump(mode="json"),
            "source_of_truth": "brief",
            "read_only": True,
            "is_qualifying": False,
            "is_recommendation": False,
            "execution_authority": False,
            "guard_access": False,
        }

    return _cached(f"explanation:{key}", 15.0, compute)


@app.get("/api/directional-shadow")
def directional_shadow_status(symbol: str = "SPY"):
    """Current automated CALL/PUT/NO_TRADE proposal plus forward-shadow counts.
    Read-only: this endpoint cannot submit an order or create a Guard."""
    import directional_shadow as ds
    import entry_policy
    import wave_server
    entry_read = (entry_policy.assess_entry(
        platform.stock_data, symbol, feed=platform.feed)
        if _regular_session_active() else
        entry_policy.market_closed_assessment(symbol, feed=platform.feed))
    contracts = wave_server.list_contracts(symbol, limit=None).get("contracts", [])
    return {**ds.status(symbol),
            "current": ds.build_proposal(entry_read, contracts)}


@app.get("/api/premarket")
def premarket(symbol: str = "SPY"):
    """Trade-readiness scorecard: Regime / Direction / Confirmation / Entry
    quality / Tradability / Event risk + Data confidence. Advisory only,
    uncalibrated; degraded/unavailable inputs lower confidence, never counted
    as neutral. Separate from the 8-signal /api/precheck the journal uses."""
    if not _regular_session_active():
        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            now_et = datetime.now(timezone.utc)
        if now_et.weekday() >= 5 or now_et.hour >= 16:
            return {
                "symbol": symbol.upper(), "market_status": "closed",
                "background": "unavailable", "assessments": {},
                "data_confidence": {"score": 0.0, "band": "unavailable"},
                "formal_readiness": "NO_TRADE",
                "note": "Market closed; no fresh premarket or entry evaluation was requested.",
            }
    import premarket as pm
    return _cached(
        f"premarket:{symbol.upper()}:{platform.feed}", 55.0,
        lambda: pm.run_premarket(platform.stock_data, symbol))


@app.get("/api/feed/quality")
def feed_quality(symbol: str = "SPY", minutes: int = 960):
    """Validate SIP (new) against IEX (reference) over recent minute bars, with
    ET-session segmentation. NOTE: this is a HISTORICAL delayed comparison — it
    checks bar construction but cannot APPROVE a feed for live entry use (that
    needs a live parallel stream + quote capture). Feeds are ALWAYS requested
    explicitly (never Alpaca's subscription-dependent default) so we never
    accidentally compare SIP against SIP. Clear message if SIP isn't subscribed."""
    import feed_quality as fq
    try:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
    except Exception:
        ET = timezone.utc

    def fetch(feed_enum):
        # feed is set EXPLICITLY per request — do not rely on the default feed
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                               start=datetime.now(timezone.utc) - timedelta(minutes=minutes + 30),
                               feed=feed_enum)
        raw = platform.stock_data.get_stock_bars(req).data.get(symbol) or []
        if not raw:
            return None
        t0 = raw[0].timestamp
        out = []
        for b in raw:
            et = b.timestamp.astimezone(ET)
            out.append({"t": int(round((b.timestamp - t0).total_seconds() / 60)),
                        "close": float(b.close), "volume": float(getattr(b, "volume", 0) or 0),
                        "session": fq.session_of_et_minutes(et.hour * 60 + et.minute)})
        return out

    try:
        iex = fetch(DataFeed.IEX)
    except Exception as e:
        raise HTTPException(502, f"Couldn't fetch IEX bars: {e}")
    try:
        sip = fetch(DataFeed.SIP)
    except Exception as e:
        msg = str(e)
        if "subscription" in msg.lower() or "403" in msg:
            return {"available": False, "reason": ("SIP feed not accessible on this account. "
                    "Subscribe to Alpaca Algo Trader Plus, then re-run to validate SIP vs IEX "
                    "before building entry logic on it.")}
        raise HTTPException(502, f"Couldn't fetch SIP bars: {e}")
    if not sip or not iex:
        return {"available": False, "reason": "one or both feeds returned no bars (market closed?)"}
    return fq.feed_quality_report(sip, iex, label_new="sip", label_ref="iex",
                                  validation_mode="historical_delayed_comparison")


@app.post("/api/premarket/shadow/log")
def premarket_shadow_log(symbol: str = "SPY"):
    """Freeze today's premarket assessment for the observational forward test.
    Marked ineligible if run after the 09:25 ET cutoff. Immutable."""
    import premarket as pm, premarket_shadow as ps
    sc = pm.run_premarket(platform.stock_data, symbol)
    return ps.log_assessment(sc)


@app.get("/api/premarket/shadow/summary")
def premarket_shadow_summary(symbol: str = "SPY"):
    """Read-only descriptive summary of the shadow forward test — directional
    correctness AND decision-quality, kept separate. Observational only."""
    import premarket_shadow as ps
    return ps.shadow_summary(symbol)


@app.get("/api/premarket/shadow/session")
def premarket_shadow_session(symbol: str = "SPY", date: str = None):
    """Read-only joined view of one session's assessment + outcomes."""
    import premarket_shadow as ps
    from datetime import datetime as _dt
    md = date or _dt.now(ps.ET).date().isoformat()
    return ps.joined_view(symbol, md) or {"available": False, "reason": "no assessment on file"}


@app.get("/api/journal/score")
def journal_score(symbol: str, type: str = "stock", direction: str = None, mode: str = "paper"):
    """Historical lean from journal_model, scored against today's live precheck
    read. Abstains (available: false) below the trade-count floor — see
    journal_model.py for why that matters more than producing a number."""
    from precheck import run_precheck
    import journal_model
    current = run_precheck(platform.stock_data, symbol, type, direction)
    return journal_model.score_setup(current, mode)


@app.get("/api/journal")
def journal():
    return _cached("journal", 10.0, _journal_rows)


def _journal_rows():
    store = getattr(platform, "store", None) if platform is not None else None
    if store is not None:
        try:
            store.sync_journal(JOURNAL)
            return store.journal_rows()
        except Exception as e:
            log(f"operational journal read failed; CSV fallback active: {e}")
    if not JOURNAL.exists():
        return []
    with JOURNAL.open() as f:
        return list(csv.DictReader(f))


@app.get("/api/microread")
def microread(symbol: str = "SPY"):
    """OLS slope/imbalance read off the tick log — see micro_read.py for the
    equations and, more importantly, why this abstains on low R² instead of
    reporting a confident-looking slope drawn through noise."""
    import micro_read
    symbol = symbol.upper()
    return _cached(f"microread:{symbol}", 10.0,
                   lambda: micro_read.compute_read(symbol))


def _with_timing_gate(result, symbol):
    """Attach the tick-timing audit and, if timing is AUDITED and FAILING its
    gates, withhold the regime read entirely — bad feed timing can create
    leakage/false confidence that enough bars alone won't catch. Unaudited
    legacy data is flagged, not blocked (its quality can't be verified)."""
    import regime_model
    timing = regime_model.timing_report(symbol)
    tq = timing.get("timing_quality", {})
    if timing.get("audited") and not tq.get("passed", True):
        return {"available": False, "status": "timing_quality_failed",
                "reason": ("Tick-stream timing failed its quality gates "
                           f"({', '.join(tq.get('failed_gates', []))}). Regime read withheld "
                           "until the feed stabilizes — see timing.timing_quality."),
                "timing": timing}
    if isinstance(result, dict):
        return {**result, "timing": timing}
    return result


@app.get("/api/regime")
def regime(symbol: str = "SPY"):
    """Live filtered regime read from the 4-state HMM — descriptive only.
    Not wired into any signal weighting or position sizing; see regime_model.py."""
    import regime_model
    key = symbol.upper()
    return _cached(
        f"regime:{key}", 55.0,
        lambda: {
            **_with_timing_gate(regime_model.regime_read(key), key),
            "market_window_active": _regular_session_active(),
        })


@app.get("/api/regime/validate")
def regime_validate(symbol: str = "SPY"):
    """On-demand walk-forward validation report — whether the regime read has
    shown any genuine out-of-sample relationship to what happens next. This is
    the gate that should be checked before the regime model is ever connected
    to anything that risks money. May take several seconds to compute."""
    import regime_model
    return _with_timing_gate(regime_model.validate_regime(symbol), symbol)


@app.post("/api/regime/snapshot")
def regime_snapshot(symbol: str = "SPY", reason: str = None):
    """Write an immutable end-of-session research snapshot now. Idempotent per
    (symbol, market_date, model_version); pass reason to force a marked rerun.
    Observational only — never influences trades."""
    import session_snapshot
    return session_snapshot.take_snapshot(symbol, force_rerun=bool(reason), reason=reason)


@app.get("/api/regime/snapshots")
def regime_snapshots(symbol: str = "SPY"):
    """Read-only multi-session aggregation over immutable snapshots, with the
    pre-registered acceptance rule. This is the multi-day persistence evidence."""
    import session_snapshot
    return session_snapshot.aggregate_sessions(symbol)


@app.get("/api/regime/research")
def regime_research(symbol: str = "SPY"):
    """Full research report: label-permutation null test (iid + time-aware
    circular-shift/block) + baseline comparison (persistence, rolling-return,
    regression slope, linear probe) + multi-horizon effects with a pre-designated
    primary horizon. Answers 'does the HMM beat cheap observable rules and chance',
    NOT 'should we trade it'. Deliberately NOT connected to sizing or signal
    weighting. Heavy — can take tens of seconds to minutes once there's real
    history; runs in a worker thread so it won't stall the price loops."""
    import regime_research
    return _with_timing_gate(regime_research.research_report(symbol), symbol)


@app.get("/api/ticks")
def ticks(symbol: str = "SPY", limit: int = 200):
    """Recent rows from the standing tick logger — raw material for testing
    whether recent price action predicts anything, not a prediction itself."""
    if not TICK_LOG.exists():
        return {"symbol": symbol, "rows": []}
    with TICK_LOG.open() as f:
        rows = [r for r in csv.DictReader(f) if r.get("symbol") == symbol.upper()]
    return {"symbol": symbol.upper(), "count": len(rows), "rows": rows[-limit:]}


def _stats_payload(mode="paper"):
    """Win/loss and P&L analytics computed from the journal, filtered by mode."""
    mode_rows = [r for r in _cached("journal", 10.0, _journal_rows)
                 if r.get("mode", "paper") == mode]
    excluded = sum(
        1 for r in mode_rows
        if r.get("scope_class") == manual_scope_policy.SCOPE_OUT_OF_ENVELOPE)
    rows = [
        r for r in mode_rows
        if r.get("scope_class") != manual_scope_policy.SCOPE_OUT_OF_ENVELOPE]
    if not rows:
        return {"count": 0, "excluded_out_of_envelope": excluded}

    def num(r, k):
        try:
            return float(r.get(k, 0) or 0)
        except ValueError:
            return 0.0

    # per-trade realized dollars: (exit - entry) * qty. Options are per-share ×100.
    trades = []
    for r in rows:
        entry, exit_ = num(r, "entry"), num(r, "exit")
        qty = num(r, "qty")
        realized_pct = num(r, "realized_pct")
        is_option = len(r.get("symbol", "")) > 8 and any(c in r["symbol"] for c in "CP")
        mult = 100 if is_option else 1
        pnl = (exit_ - entry) * qty * mult
        trades.append({
            "symbol": r.get("symbol", ""),
            "time": r.get("utc_time", ""),
            "entry": entry, "exit": exit_, "qty": qty,
            "peak_pct": num(r, "peak_pct"),
            "realized_pct": realized_pct,
            "pnl": pnl,
            "reason": r.get("reason", ""),
            "win": realized_pct > 0,
        })

    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    total_pnl = sum(t["pnl"] for t in trades)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = sum(t["pnl"] for t in losses)

    def avg(lst, k):
        return sum(x[k] for x in lst) / len(lst) if lst else 0.0

    return {
        "count": len(trades),
        "excluded_out_of_envelope": excluded,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win_pct": round(avg(wins, "realized_pct"), 3),
        "avg_loss_pct": round(avg(losses, "realized_pct"), 3),
        "avg_win_dollars": round(avg(wins, "pnl"), 2),
        "avg_loss_dollars": round(avg(losses, "pnl"), 2),
        "best": max(trades, key=lambda t: t["realized_pct"]) if trades else None,
        "worst": min(trades, key=lambda t: t["realized_pct"]) if trades else None,
        "profit_factor": round(gross_win / abs(gross_loss), 2) if gross_loss else None,
        "avg_peak_pct": round(avg(trades, "peak_pct"), 3),
        "trades": list(reversed(trades)),
    }


@app.get("/api/stats")
def stats(mode: str = "paper"):
    return _cached(f"stats:{mode}", 10.0, lambda: _stats_payload(mode))


@app.get("/api/dashboard-snapshot")
def dashboard_snapshot():
    """Compact, ready-to-serve read model; no raw tick/history payloads."""
    store = getattr(platform, "store", None) if platform is not None else None
    storage = _cached("storage_status", 60.0, store.status) if store else {
        "available": False, "reason": "operational_store_unavailable"
    }
    cache = getattr(platform, "dashboard_cache", None) if platform is not None else None
    def safe(component, read):
        try:
            return read()
        except Exception as e:
            return {"available": False, "component": component, "error": str(e)[:160]}
    return {
        "snapshot_version": "dashboard-snapshot-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "state": safe("state", state),
        "holdings": safe("holdings", holdings),
        "stats": safe("stats", lambda: stats("paper")),
        "microread": safe("microread", lambda: microread("SPY")),
        "wave": safe("wave", wave_status),
        "flow": safe("flow", flow_ranking),
        "storage": storage,
        "cache": cache.metadata() if cache else {},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--sip", action="store_true",
                    help="Use the paid SIP feed (100%% market volume). Requires "
                         "Alpaca Algo Trader Plus. Without this flag, the free IEX feed is used.")
    ap.add_argument("--port", type=int, default=8420)
    args = ap.parse_args()
    if args.live:
        sys.exit("Live mode is physically blocked; Scalpr V2 is paper/shadow only.")
    feed = "sip" if args.sip else "iex"
    global platform
    platform = Platform(live=args.live, feed=feed)
    log(f"SCALPR TRADE platform on http://localhost:{args.port} "
        f"[{'LIVE' if args.live else 'PAPER'}] [{feed.upper()} feed]")
    if feed == "sip":
        log("SIP feed requested — make sure Algo Trader Plus is active on your Alpaca account.")
    log("Keep this window open — closing it stops the platform.")
    threading.Timer(1.5, lambda: __import__("webbrowser").open(
        f"http://localhost:{args.port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
