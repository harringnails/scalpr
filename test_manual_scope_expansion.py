"""Network-free safety tests for ADR-019 manual scope expansion."""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

import journal_model
import manual_scope_policy as msp
import scalp_server as ss
import scope_policy


NARROW_SCOPE_SHA256 = "41f66d9821c0e034686b40e619868ee79e4b894ecfae37d2c0dbb2f494d4ee62"


def occ(root="AMD", dte=30, right="C", strike=200.0, as_of=None):
    as_of = as_of or scope_policy.market_date()
    expiry = as_of + timedelta(days=dte)
    return f"{root}{expiry.strftime('%y%m%d')}{right}{int(strike * 1000):08d}"


class Health:
    def __init__(self):
        self.events = []

    def record(self, component, status, **kwargs):
        self.events.append((component, status, kwargs))


class Cache:
    def invalidate(self, *keys):
        return None


class FakeTrading:
    def __init__(self, symbol, *, contract_exists=True, contract_tradable=True,
                 asset_class="us_equity", live_position_qty=0):
        parsed = scope_policy.parse_occ(symbol)
        self.symbol = symbol
        self.parsed = parsed
        self.contract_exists = contract_exists
        self.contract_tradable = contract_tradable
        self.asset_class = asset_class
        self.qty = float(live_position_qty)
        self.avg = 1.0
        self.submits = []
        self.contract_requests = []

    def get_asset(self, symbol):
        assert symbol == self.parsed["underlying"]
        return SimpleNamespace(asset_class=self.asset_class, status="active", tradable=True)

    def get_option_contract(self, symbol):
        if not self.contract_exists:
            raise RuntimeError("not found")
        assert symbol == self.symbol
        return SimpleNamespace(
            symbol=symbol, underlying_symbol=self.parsed["underlying"],
            expiration_date=self.parsed["expiry"],
            type="call" if self.parsed["right"] == "C" else "put",
            status="active", tradable=self.contract_tradable,
        )

    def get_option_contracts(self, req):
        self.contract_requests.append(req)
        c = self.get_option_contract(self.symbol)
        c.strike_price = self.parsed["strike"]
        return SimpleNamespace(option_contracts=[c])

    def get_clock(self):
        return SimpleNamespace(is_open=True)

    def submit_order(self, req):
        self.submits.append(req)
        if getattr(req.side, "value", req.side) == "buy":
            self.qty += float(req.qty)
        return SimpleNamespace(id="order-1")

    def get_order_by_id(self, _order_id):
        return SimpleNamespace(
            status=SimpleNamespace(value="filled"),
            filled_avg_price="1.00", filled_qty=str(self.submits[-1].qty),
        )

    def get_open_position(self, symbol):
        assert symbol == self.symbol
        return SimpleNamespace(symbol=symbol, qty=self.qty, avg_entry_price=self.avg)


def config(symbol, *, climb=False):
    return {
        "symbol": symbol, "type": "option", "buy": True, "qty": 1,
        "auto_exit_authorized": True,
        "ladder": [{"at": 0, "tol": 15}, {"at": 15, "tol": 6}],
        "grace_seconds": 0, "confirm_ticks": 2,
        "climb_adds": {"enabled": climb, "add_contracts": 1, "max_adds": 2},
    }


def platform(symbol, **trading_kwargs):
    p = object.__new__(ss.Platform)
    p.live = False
    p.guards = {}
    p.trading = FakeTrading(symbol, **trading_kwargs)
    p.health = Health()
    p.dashboard_cache = Cache()
    p._capture_manual_discretionary_decision = lambda **_kwargs: None
    return p


def test_narrow_automated_policy_is_byte_for_byte_unchanged_and_default():
    digest = hashlib.sha256(Path("scope_policy.py").read_bytes()).hexdigest()
    assert digest == NARROW_SCOPE_SHA256
    symbol = occ("AMD", 30)
    p = platform(symbol)
    p._start_entry_enrichment = lambda _guard: None
    try:
        p.open_trade(config(symbol))  # no manual=True: fail-closed narrow default
    except ss.HTTPException as exc:
        assert exc.status_code == 422 and "SPY only" in exc.detail
    else:
        raise AssertionError("default order path widened beyond SPY 0-2 DTE")
    assert p.trading.submits == []


def test_manual_policy_accepts_zero_to_sixty_dte_options_only():
    as_of = date(2026, 8, 6)
    assert msp.validate_manual_option(occ("AMD", 0, as_of=as_of), as_of)["dte"] == 0
    assert msp.validate_manual_option(occ("AMD", 60, as_of=as_of), as_of)["dte"] == 60
    assert msp.validate_manual_option(occ("SPY", 2, as_of=as_of), as_of)["scope_class"] == "validated"
    assert msp.validate_manual_option(occ("AMD", 2, as_of=as_of), as_of)["scope_class"] == "manual_out_of_envelope"
    for symbol in (occ("AMD", 61, as_of=as_of), occ("AMD", -1, as_of=as_of)):
        try:
            msp.validate_manual_option(symbol, as_of)
        except msp.ManualScopeError:
            pass
        else:
            raise AssertionError(f"manual DTE boundary accepted {symbol}")
    try:
        msp.validate_manual_trade("AMD", "stock", as_of)
    except msp.ManualScopeError as exc:
        assert "options only" in str(exc)
    else:
        raise AssertionError("manual stock trade was accepted")


def test_manual_out_of_envelope_trade_is_broker_validated_guarded_and_isolated():
    symbol = occ("AMD", 30)
    p = platform(symbol)
    enrichment = []
    p._start_entry_enrichment = enrichment.append
    result = p.open_trade(config(symbol), manual=True)
    assert len(p.trading.submits) == 1
    assert result["scope_class"] == "manual_out_of_envelope"
    assert result["scope_version"] == msp.SCOPE_VERSION
    assert result["entry_source"] == "manual_builder"
    assert p.guards[symbol].scope_class == "manual_out_of_envelope"
    assert enrichment == []
    assert any(c == "research_isolation" and s == "EXCLUDED"
               for c, s, _ in p.health.events)


def test_missing_untradeable_or_non_equity_contract_fails_before_order():
    symbol = occ("AMD", 30)
    for kwargs in ({"contract_exists": False}, {"contract_tradable": False},
                   {"asset_class": "crypto"}):
        p = platform(symbol, **kwargs)
        p._start_entry_enrichment = lambda _guard: None
        try:
            p.open_trade(config(symbol), manual=True)
        except ss.HTTPException as exc:
            assert exc.status_code == 422
        else:
            raise AssertionError(f"invalid Alpaca contract accepted: {kwargs}")
        assert p.trading.submits == []


def test_manual_expansion_and_climb_adds_are_blocked_in_live_or_outside_baseline():
    symbol = occ("AMD", 30)
    p = platform(symbol)
    p.live = True
    try:
        p.open_trade(config(symbol), manual=True)
    except ss.HTTPException as exc:
        assert exc.status_code == 422 and "live mode" in exc.detail
    else:
        raise AssertionError("expanded manual live trade accepted")
    assert p.trading.submits == []

    p.live = False
    try:
        p.open_trade(config(symbol, climb=True), manual=True)
    except ss.HTTPException as exc:
        assert exc.status_code == 422 and "SPY 0-2 DTE" in exc.detail
    else:
        raise AssertionError("out-of-envelope automated climb accepted")
    assert p.trading.submits == []


def test_manual_chain_queries_sixty_days_and_only_returns_tradable_contracts():
    symbol = occ("AMD", 30)
    p = platform(symbol)
    expirations = p.list_expirations("AMD", "C")
    assert expirations == [scope_policy.parse_occ(symbol)["expiry"].isoformat()]
    req = p.trading.contract_requests[-1]
    assert (req.expiration_date_lte - req.expiration_date_gte).days == 60


def test_journal_model_and_validated_stats_exclude_manual_out_of_envelope():
    fields = ["mode", "symbol", "entry", "exit", "qty", "peak_pct",
              "realized_pct", "reason", "entry_verdict", "scope_class"]
    rows = [
        {"mode": "paper", "symbol": occ("SPY", 1), "entry": "1", "exit": "2",
         "qty": "1", "peak_pct": "100", "realized_pct": "100", "reason": "manual",
         "entry_verdict": "ok", "scope_class": "validated"},
        {"mode": "paper", "symbol": occ("AMD", 30), "entry": "1", "exit": "9",
         "qty": "1", "peak_pct": "800", "realized_pct": "800", "reason": "manual",
         "entry_verdict": "ok", "scope_class": "manual_out_of_envelope"},
    ]
    import csv
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "journal.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
        old_journal = journal_model.JOURNAL
        try:
            journal_model.JOURNAL = path
            labeled = journal_model._load_labeled_trades("paper")
        finally:
            journal_model.JOURNAL = old_journal
        assert len(labeled) == 1 and labeled[0]["scope_class"] == "validated"

    old_cached, old_rows = ss._cached, ss._journal_rows
    try:
        ss._cached = lambda _key, _ttl, compute: compute()
        ss._journal_rows = lambda: rows
        stats = ss._stats_payload("paper")
    finally:
        ss._cached, ss._journal_rows = old_cached, old_rows
    assert stats["count"] == 1
    assert stats["excluded_out_of_envelope"] == 1
    assert stats["best"]["symbol"].startswith("SPY")


def test_dashboard_endpoint_is_the_only_explicit_manual_opt_in():
    calls = []
    fake = SimpleNamespace(open_trade=lambda cfg, **kwargs: calls.append((cfg, kwargs)) or {"ok": True})
    original = ss.platform
    try:
        ss.platform = fake
        assert ss.create_trade({"symbol": occ("AMD", 30), "ladder": []}) == {"ok": True}
    finally:
        ss.platform = original
    assert calls[0][1] == {"manual": True}


def test_fresh_server_startup_fails_closed_if_holdings_exist_or_are_unverifiable():
    assert ss._require_flat_paper_account(
        SimpleNamespace(get_all_positions=lambda: [])) == 0
    for trading in (
        SimpleNamespace(get_all_positions=lambda: [SimpleNamespace(symbol="AMD")]),
        SimpleNamespace(get_all_positions=lambda: (_ for _ in ()).throw(RuntimeError("offline"))),
    ):
        try:
            ss._require_flat_paper_account(trading)
        except RuntimeError as exc:
            assert "Startup blocked" in str(exc)
        else:
            raise AssertionError("unsafe fresh startup was accepted")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
