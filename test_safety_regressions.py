"""Network-free regressions for the 2026-08-05 Guard/UI incidents."""

import inspect
import math
import os
from datetime import datetime, time as datetime_time, timezone

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

import incubation_server
import scalp_server as ss
import scope_policy


class Health:
    def __init__(self): self.events = []
    def record(self, component, status, **kwargs):
        self.events.append((component, status, kwargs))


class Cache:
    def invalidate(self, *keys): pass


class Status:
    value = "filled"


class Order:
    id = "order-1"
    status = Status()
    filled_avg_price = 1.00
    filled_qty = 2


class Position:
    def __init__(self, symbol, qty, avg):
        self.symbol, self.qty, self.avg_entry_price = symbol, qty, avg


class Trading:
    def __init__(self, symbol, qty=0, avg=1.0):
        self.symbol, self.qty, self.avg = symbol, qty, avg
        self.submits = []
    def get_clock(self): return type("Clock", (), {"is_open": True})()
    def submit_order(self, req):
        self.submits.append(req)
        if getattr(req, "side", None) == ss.OrderSide.BUY:
            add = float(req.qty)
            self.avg = ((self.avg * self.qty) + 1.0 * add) / (self.qty + add) if self.qty else 1.0
            self.qty += add
        return Order()
    def get_order_by_id(self, _):
        o = Order()
        o.filled_qty = self.submits[-1].qty
        return o
    def get_open_position(self, symbol):
        assert symbol == self.symbol
        return Position(symbol, self.qty, self.avg)


class ClockFailureTrading(Trading):
    def get_clock(self):
        raise ConnectionError("clock unavailable")


class ReadOnlyAccountTrading:
    def __init__(self, positions=0, open_orders=0):
        self.positions = [object()] * positions
        self.open_orders = [object()] * open_orders
        self.calls = []
    def get_account(self):
        self.calls.append("get_account")
        status = type("AccountStatus", (), {"value": "ACTIVE"})()
        return type("Account", (), {"id": "paper-account-id", "status": status})()
    def get_all_positions(self):
        self.calls.append("get_all_positions")
        return self.positions
    def get_orders(self, request):
        self.calls.append(("get_orders", request.status))
        return self.open_orders


class ReadOnlyCalendarTrading:
    def __init__(self, *, trading_day=True):
        self.trading_day = trading_day
        self.calls = []

    def get_calendar(self, request):
        self.calls.append((request.start, request.end))
        if not self.trading_day:
            return []
        return [type("Session", (), {
            "date": request.start,
            "open": datetime_time(9, 30),
            "close": datetime_time(16, 0),
        })()]


def symbol_today():
    return f"SPY{scope_policy.market_date().strftime('%y%m%d')}C00600000"


def test_feed_quality_has_timedelta_available():
    """The SIP-vs-IEX probe must reach Alpaca instead of failing locally."""
    assert ss.timedelta(minutes=1).total_seconds() == 60


def test_load_keys_requires_env_and_refuses_plaintext_fallback():
    old_key = os.environ.pop("ALPACA_API_KEY", None)
    old_secret = os.environ.pop("ALPACA_SECRET_KEY", None)
    try:
        try:
            ss.load_keys()
        except RuntimeError as exc:
            assert "Keychain" in str(exc)
        else:
            raise AssertionError("load_keys accepted missing env credentials")
        os.environ["ALPACA_API_KEY"] = "env-key"
        os.environ["ALPACA_SECRET_KEY"] = "env-secret"
        assert ss.load_keys() == ("env-key", "env-secret")
    finally:
        if old_key is not None:
            os.environ["ALPACA_API_KEY"] = old_key
        else:
            os.environ.pop("ALPACA_API_KEY", None)
        if old_secret is not None:
            os.environ["ALPACA_SECRET_KEY"] = old_secret
        else:
            os.environ.pop("ALPACA_SECRET_KEY", None)


def test_asymmetric_hard_stop_requires_two_fresh_ticks_below_cap():
    sym = symbol_today()
    guard = ss.AsymmetricHardStopGuard(hard_stop_cfg(sym), 1.0, 2)
    original_time = ss.time.time
    try:
        now = [1000.0]
        ss.time.time = lambda: now[0]
        guard.opened_at = now[0] - 1.0
        assert guard.on_price(1.00) is None
        now[0] += 1.0
        assert guard.on_price(0.74) is None
        now[0] += 1.0
        reason = guard.on_price(0.72)
        assert reason and "hard-stop: loss cap" in reason
    finally:
        ss.time.time = original_time


def test_runner_measurement_wrapper_preserves_asymmetric_loss_cap():
    sym = symbol_today()
    bare = ss.AsymmetricHardStopGuard(hard_stop_cfg(sym), 1.0, 2)
    wrapped_cfg = hard_stop_cfg(sym)
    wrapped_cfg["runner_policy"] = {
        "enabled": True,
        "version": "regime-flow-runner-v1",
        "confirm_observations": 1,
    }
    wrapped = ss.AsymmetricHardStopGuard(wrapped_cfg, 1.0, 2)
    original_time = ss.time.time
    try:
        now = [1500.0]
        ss.time.time = lambda: now[0]
        bare.opened_at = wrapped.opened_at = now[0] - 1.0
        for price in (1.00, 0.74, 0.72):
            assert bare.on_price(price) == wrapped.on_price(price)
            now[0] += 1.0
        assert wrapped.runner_measurement is not None
        assert "hard-stop: loss cap" in wrapped.on_price(0.72)
    finally:
        ss.time.time = original_time


def test_asymmetric_hard_stop_exits_after_sustained_bad_quotes():
    sym = symbol_today()
    guard = ss.AsymmetricHardStopGuard(hard_stop_cfg(sym), 1.0, 2)
    original_time = ss.time.time
    try:
        now = [2000.0]
        ss.time.time = lambda: now[0]
        guard.opened_at = now[0] - 1.0
        assert guard.on_price(1.00) is None
        now[0] += 4.0
        assert guard.mark_quote_unavailable("NO_BID", "quote missing") is None
        now[0] += 4.0
        assert guard.mark_quote_unavailable("NO_BID", "quote missing") is None
        now[0] += 9.0
        reason = guard.mark_quote_unavailable("NO_BID", "quote missing")
        assert reason and "quote degraded" in reason
    finally:
        ss.time.time = original_time


def test_asymmetric_hard_stop_cannot_be_bypassed_by_pause_resume():
    sym = symbol_today()
    guard = ss.AsymmetricHardStopGuard(hard_stop_cfg(sym), 1.0, 2)
    original_time = ss.time.time
    try:
        now = [3000.0]
        ss.time.time = lambda: now[0]
        guard.opened_at = now[0] - 1.0
        assert guard.on_price(1.00) is None
        guard.paused = True
        now[0] += 1.0
        assert guard.on_price(0.74) is None
        now[0] += 1.0
        reason = guard.on_price(0.72)
        assert reason and "hard-stop: loss cap" in reason
    finally:
        ss.time.time = original_time


def test_account_flat_proof_is_direct_read_only_and_redacts_identity():
    p = object.__new__(ss.Platform)
    p.live = False
    p.trading = ReadOnlyAccountTrading()
    proof = p.account_flat_proof()
    assert proof["mode"] == "paper"
    assert proof["source"] == "alpaca_trading_api_direct_uncached"
    assert proof["positions_count"] == proof["open_orders_count"] == 0
    assert proof["flat"] is True
    assert proof["account_identity_sha256"] != "paper-account-id"
    assert len(proof["account_identity_sha256"]) == 64
    assert p.trading.calls[0:2] == ["get_account", "get_all_positions"]
    assert p.trading.calls[2][0] == "get_orders"
    assert "_cached" not in inspect.getsource(ss.account_flat_proof_endpoint)


def test_market_calendar_proof_is_direct_uncached_and_identifies_holiday():
    p = object.__new__(ss.Platform)
    p.trading = ReadOnlyCalendarTrading()
    proof = p.market_calendar_proof("2026-08-10")
    assert proof["source"] == "alpaca_trading_calendar_direct_uncached"
    assert proof["market"] == "XNYS" and proof["is_trading_day"] is True
    assert proof["market_open_utc"].startswith("2026-08-10T13:30:00")
    assert p.trading.calls == [(datetime(2026, 8, 10).date(),
                                datetime(2026, 8, 10).date())]
    p.trading = ReadOnlyCalendarTrading(trading_day=False)
    assert p.market_calendar_proof("2026-08-09")["is_trading_day"] is False
    assert "_cached" not in inspect.getsource(ss.market_calendar_proof_endpoint)


def test_trader_plus_startup_fallback_is_visible_in_state_source():
    """A SIP grant race must be surfaced instead of silently degrading quotes."""
    source = inspect.getsource(ss.Platform.__init__)
    assert 'self.feed = "iex"' in source
    assert "real-time grant is not active" in source
    assert "feed_startup_notice" in inspect.getsource(ss.state)


def cfg(symbol, qty=2):
    return {"symbol": symbol, "type": "option", "buy": True, "qty": qty,
            "auto_exit_authorized": True,
            "ladder": [{"at": 0, "tol": 15}, {"at": 15, "tol": 6}],
            "grace_seconds": 0, "confirm_ticks": 2}


def hard_stop_cfg(symbol, qty=2):
    spec = cfg(symbol, qty)
    spec.update({
        "hard_stop_mode": "asymmetric_hard_stop_v1",
        "hard_stop_loss_pct": 25,
        "hard_stop_profit_activation_pct": 20,
        "hard_stop_peak_giveback_pct": 10,
        "hard_stop_quote_degradation_seconds": 12,
        "hard_stop_loss_cap_confirmation_ticks": 2,
    })
    return spec


def runner_observation(*, flow_direction="BULLISH"):
    observed = datetime.now(timezone.utc).isoformat()
    return {
        "regime": {
            "schema_version": ss.runner_policy.REGIME_SOURCE_VERSION,
            "available": True,
            "status": "FRESH",
            "state": "TREND_UP",
            "metadata": {"fit_end_time": observed},
        },
        "flow": {
            "as_of": observed,
            "fresh": True,
            "tier": "GREEN",
            "direction": flow_direction,
        },
    }


def test_runner_observation_uses_cached_deterministic_regime_inputs():
    observed = datetime.now(timezone.utc).isoformat()

    class Collector:
        config = {"symbol": "SPY"}

        @staticmethod
        def regime_inputs_snapshot():
            return {
                "minute_bars": [{"t": 0, "close": 100.0}],
                "now_minute": 5,
                "prior_session_minute_bars": [[{"t": 0, "close": 99.0}]],
                "observed_at": observed,
            }

    class FlowStore:
        @staticmethod
        def latest_snapshot(**_kwargs):
            return {
                "institutional_flow_status": "AVAILABLE",
                "window_end": observed,
                "event_count": 3,
                "flow_direction_score": 1.0,
            }

    platform = object.__new__(ss.Platform)
    platform.entry_bid_collector = Collector()
    platform.flow_store = FlowStore()
    calls = []
    original = ss.regime_layer_v0.classify_regime
    try:
        def classify(**kwargs):
            calls.append(kwargs)
            return {
                "schema_version": ss.runner_policy.REGIME_SOURCE_VERSION,
                "status": "FRESH", "state": "TREND_UP",
                "as_of_completed_bucket": 0,
                "execution_authority": False,
            }

        ss.regime_layer_v0.classify_regime = classify
        result = platform._runner_observation("SPY")
    finally:
        ss.regime_layer_v0.classify_regime = original

    assert calls == [{
        "minute_bars": [{"t": 0, "close": 100.0}],
        "now_minute": 5,
        "prior_session_minute_bars": [[{"t": 0, "close": 99.0}]],
    }]
    assert result["regime"]["available"] is True
    assert result["regime"]["metadata"]["fit_end_time"] == observed
    assert result["regime"]["metadata"]["source"] == "regime_layer_v0.classify_regime"
    assert "regime_model" not in inspect.getsource(ss.Platform._runner_observation)


def platform(symbol, qty=0):
    p = object.__new__(ss.Platform)
    p.live = False
    p.guards = {}
    p.trading = Trading(symbol, qty=qty)
    p.health = Health()
    p.dashboard_cache = Cache()
    return p


def test_guard_exists_before_optional_enrichment_starts():
    sym = symbol_today()
    p = platform(sym)
    seen = []
    def start(guard):
        seen.append(guard)
        assert p.guards[sym] is guard
    p._start_entry_enrichment = start
    result = p.open_trade(cfg(sym))
    assert seen and result["symbol"] == sym
    assert p.guards[sym].entry_signals is None


def test_open_trade_routes_to_asymmetric_hard_stop_guard_when_configured():
    sym = symbol_today()
    p = platform(sym)
    p._start_entry_enrichment = lambda guard: None
    result = p.open_trade(hard_stop_cfg(sym))
    guard = p.guards[sym]
    assert guard.__class__.__name__ == "AsymmetricHardStopGuard"
    assert result["risk_mode"] == "asymmetric_hard_stop_v1"
    assert result["hard_stop_loss_pct"] == 25


def test_runner_policy_is_opt_in_and_reverts_to_static_ladder_on_conflict():
    sym = symbol_today()
    regular = ss.Guard(cfg(sym), 1.0, 2)
    regular.on_price(1.20)
    assert regular.tolerance() == 6
    runner_cfg = cfg(sym)
    runner_cfg["runner_policy"] = {
        "enabled": True,
        "version": "regime-flow-runner-v1",
        "confirm_observations": 1,
    }
    guarded = ss.Guard(runner_cfg, 1.0, 2)
    guarded.on_price(1.20)
    guarded.update_runner_observation(runner_observation())
    assert guarded.snapshot()["runner_policy"]["status"] == "ACTIVE"
    assert math.isclose(guarded.tolerance(), 10)
    guarded.update_runner_observation(runner_observation(flow_direction="BEARISH"))
    assert guarded.snapshot()["runner_policy"]["status"] == "BASELINE"
    assert guarded.tolerance() == 6


def test_duplicate_entry_is_blocked_and_audited_without_broker_order():
    sym = symbol_today()
    p = platform(sym)
    p.guards[sym] = ss.Guard(cfg(sym), 1.0, 2)
    try:
        p.open_trade(cfg(sym))
    except ss.HTTPException as exc:
        assert exc.status_code == 409 and "Add contracts" in exc.detail
    else:
        raise AssertionError("duplicate guarded entry was accepted")
    assert not p.trading.submits
    assert any(status == "ORDER_BLOCKED" for _, status, _ in p.health.events)


def test_trade_without_explicit_auto_exit_authorization_is_blocked():
    sym = symbol_today()
    p = platform(sym)
    spec = cfg(sym)
    spec["auto_exit_authorized"] = False
    try:
        p.open_trade(spec)
    except ss.HTTPException as exc:
        assert exc.status_code == 409 and "Automatic whole-position exits" in exc.detail
    else:
        raise AssertionError("trade without automatic-exit authorization was accepted")
    assert not p.trading.submits
    assert any(status == "ORDER_BLOCKED" for _, status, _ in p.health.events)


def test_market_clock_failure_blocks_order_submission():
    sym = symbol_today()
    p = platform(sym)
    p.trading = ClockFailureTrading(sym)
    try:
        p.open_trade(cfg(sym))
    except ss.HTTPException as exc:
        assert exc.status_code == 503
        assert "verify that the market is open" in exc.detail
    else:
        raise AssertionError("order proceeded without verified market status")
    assert not p.trading.submits
    blocked = [event for event in p.health.events if event[1] == "ORDER_BLOCKED"]
    assert blocked and blocked[-1][2]["fields"]["error_type"] == "ConnectionError"


def test_restart_requires_direct_flat_account_and_open_order_proof():
    source = open("restart.sh", encoding="utf-8").read()
    assert "/api/account-flat-proof" in source
    assert "/api/holdings" not in source
    assert 'proof.get("positions_count") == 0' in source
    assert 'proof.get("open_orders_count") == 0' in source
    assert 'proof.get("mode") == "paper"' in source


def test_cancelled_provider_subscriptions_stay_disabled_in_restart_paths():
    for path in ("restart.sh", "restart_cron.sh"):
        source = open(path, encoding="utf-8").read()
        assert "SCALPR_UW_INGESTION_ENABLED=0" in source
        assert "SCALPR_IVOL_CAPTURE_ENABLED=0" in source
        assert "SCALPR_UW_INGESTION_ENABLED=1" not in source
        assert "SCALPR_IVOL_CAPTURE_ENABLED=1" not in source


def test_cron_restart_has_safe_proof_and_runtime_flag_parity():
    source = open("restart_cron.sh", encoding="utf-8").read()
    assert "/api/account-flat-proof" in source
    assert "/api/holdings" not in source
    assert 'proof.get("positions_count") == 0' in source
    assert 'proof.get("open_orders_count") == 0' in source
    assert 'proof.get("mode") == "paper"' in source
    for flag in (
        "WAVE_RIDING_ENABLED=1",
        "INCUBATION_SHADOW_ENABLED=1",
        "SCALPR_UW_INGESTION_ENABLED=0",
        "SCALPR_IVOL_CAPTURE_ENABLED=0",
        "SCALPR_CLOUDSQL_MIRROR_ENABLED=1",
        "ENTRY_INTEL_BID_CAPTURE_ENABLED=1",
        "EXPLAIN_LAYER_ENABLED=0",
    ):
        assert flag in source
    assert 'status.get("collector_version") == "entry-bid-collector-v1.2"' in source
    assert 'status.get("collection_role") == "PRELOCK_DRY_RUN"' in source
    assert 'status.get("cohorts_locked") is False' in source
    assert 'status.get("execution_authority") is False' in source
    assert 'status.get("guard_access") is False' in source


def test_cloudsql_stale_status_never_reports_no_backlog():
    now = datetime(2026, 8, 13, 23, 0, tzinfo=timezone.utc)
    result = ss._cloudsql_status_with_freshness({
        "status": "OK",
        "updated_at": "2026-08-12T23:00:00+00:00",
        "backlog_remaining": False,
    }, now=now)
    assert result["status"] == "STALE"
    assert result["reported_status"] == "OK"
    assert result["fresh"] is False
    assert result["backlog_remaining"] is None


def test_resume_fails_closed_without_typed_symbol_and_records_confirmed_resume():
    sym = symbol_today()
    p = platform(sym, qty=2)
    guard = ss.Guard(cfg(sym), 1.0, 2)
    guard.on_price(1.10)
    guard.paused = True
    p.guards[sym] = guard
    prior_platform, prior_capture = ss.platform, ss._capture_guard_event
    captured = []
    try:
        ss.platform = p
        ss._capture_guard_event = lambda *a, **k: captured.append((a, k))
        try:
            ss.resume_guard(sym, {})
        except ss.HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("bare resume request was accepted")
        assert guard.paused is True
        result = ss.resume_guard(sym, {
            "confirm_symbol": sym,
            "allow_automatic_sell": True,
            "request_id": "resume-test",
        })
        assert result["ok"] is True and guard.paused is False
        assert captured[-1][1]["actor"] == "dashboard_resume_confirmed"
        assert captured[-1][1]["request_id"] == "resume-test"
    finally:
        ss.platform, ss._capture_guard_event = prior_platform, prior_capture


def test_explicit_add_reconciles_guard_quantity_and_average():
    sym = symbol_today()
    p = platform(sym, qty=2)
    guard = ss.Guard(cfg(sym), 1.0, 2)
    guard.on_price(1.10)
    p.guards[sym] = guard
    result = p.add_to_guard(sym, 1)
    assert result["position"]["qty"] == 3
    assert guard.qty == 3 and guard.entry == p.trading.avg
    fills = [e for e in p.health.events if e[1] == "FILLED"]
    assert fills[-1][2]["fields"]["initiated_by"] == "dashboard_manual_add"


def test_guard_loop_contains_no_research_or_add_observers():
    source = inspect.getsource(ss.Platform._poll_loop)
    assert "observer_tick" not in source
    assert "_climb_add_tick" not in source


def test_incubation_worker_is_bounded_to_requested_trade_count():
    now = datetime.now(timezone.utc).isoformat()
    records = {}
    guards = {}
    for i in range(3):
        sym = f"SPY{i}"
        opened = f"opened-{i}"
        tid = f"{sym}:{opened}"
        records[tid] = {"trade_id": tid, "state": "OBSERVING_PRE_EXIT",
                        "contract_symbol": sym, "underlying_symbol": "SPY",
                        "created_at": now}
        guards[sym] = type("Guard", (), {"opened": opened, "done": False,
                                          "snapshot": lambda self: {"peak": 0}})()
    calls = []
    fake_store = type("Store", (), {
        "list_active": staticmethod(lambda: list(records)),
        "load_trade": staticmethod(lambda tid: records.get(tid)),
        "unregister_active": staticmethod(lambda tid: None),
        "append_telemetry": staticmethod(lambda *a, **k: None),
        "last_sequence": staticmethod(lambda tid: 0),
    })
    original_store, original_enabled = incubation_server.ist, incubation_server.icfg.enabled
    original_quote, original_observe = incubation_server._quote_obs, incubation_server.ish.observe
    try:
        incubation_server.ist = fake_store
        incubation_server.icfg.enabled = lambda: True
        incubation_server._quote_obs = lambda *a, **k: calls.append(a[1]) or {
            "ts": now, "option_quote_timestamp": now, "underlying_quote_timestamp": now}
        incubation_server.ish.observe = lambda *a, **k: {"applied": True}
        p = type("Platform", (), {"guards": guards, "_last_open_symbols": set()})()
        result = incubation_server.observer_tick(p, max_trades=1)
        assert result["selected"] == 1 and len(calls) == 1
    finally:
        incubation_server.ist, incubation_server.icfg.enabled = original_store, original_enabled
        incubation_server._quote_obs, incubation_server.ish.observe = original_quote, original_observe


def test_dashboard_exposes_safety_and_one_click_controls():
    html = ss.DASHBOARD.read_text()
    assert 'id="guardSafety"' in html
    assert 'id="researchDiagnostics"' in html
    assert "Research diagnostics — non-blocking, no approval required" in html
    assert "if (!researchDiagnosticsOpen()) return" in html
    assert 'id="exploratorySummaryDetails"' in html
    assert "addToGuard" in html
    assert "UNPROTECTED" in html
    assert 'id="autoExitAck"' not in html
    assert 'id="goBtn" onclick="submitTrade()">ENGAGE SCALPER</button>' in html
    assert "Auto-exit authorized: the entire remaining position may be sold" in html
    assert "allow_automatic_sell" in html


def test_dashboard_uses_progressive_disclosure_without_changing_trade_controls():
    html = ss.DASHBOARD.read_text()
    assert html.count('class="trade-step"') == 4
    assert 'class="trade-step experimental-step"' in html
    assert 'id="exitSettings"' in html
    assert 'id="experimentalSettings"' in html
    assert 'id="tradeReview"' in html
    assert 'id="type" hidden' in html
    assert '<details class="manual-entry">' in html
    assert '<details class="settings-panel" id="exitSettings">' in html
    assert '<details class="settings-panel" id="experimentalSettings">' in html
    for element_id in (
        "symbol", "alloc", "buyMode", "ladder", "stallSec", "stallMin",
        "graceSec", "confirmTicks", "runnerEnabled", "climbEnabled",
        "climbQty", "climbMax", "goBtn", "checkBtn", "skipBtn",
    ):
        assert f'id="{element_id}"' in html


def test_dashboard_market_sign_uses_collector_window_and_fails_unlit():
    html = ss.DASHBOARD.read_text()
    assert 'class="sign mini"' in html
    assert 'id="mktOpen"' in html
    assert 'id="mktClosed"' in html
    assert "fetch('/api/entry-intelligence/collector')" in html
    assert "typeof status.market_window !== 'boolean'" in html
    assert "openWord.className = 'word' + (marketOpen === true ? ' on-green' : '')" in html
    assert "closedWord.className = 'word' + (marketOpen === false ? ' on-red' : '')" in html
    assert "renderMarketSign(null)" in html
    assert "scheduleRefresh(refreshMarketSign, 5000)" in html
    assert "@media (prefers-reduced-motion:reduce)" in html


def test_all_dashboard_trade_actions_are_one_click_without_approval_popups():
    html = ss.DASHBOARD.read_text()
    assert "Sell 100% of ${sym} at market right now?" not in html
    assert "Close your entire ${sym} position at market right now?" not in html
    assert "Close EVERY open position in your account" not in html
    assert "Add ${qty} contract${qty === 1 ? '' : 's'} to ${sym}?" not in html
    assert "Type the exact contract symbol to continue" not in html
    assert "confirm(" not in html
    assert "prompt(" not in html
    # API requests still carry explicit authorization; the clicked action is
    # the approval instead of a second browser dialog.
    assert "allow_automatic_sell: true" in html


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
            print("PASS", name)
