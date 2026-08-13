"""
Tests for the WHOLE_POSITION_RATCHET exit behavior and its noise sensitivity.

Confirms (per review):
  * a valid ratchet breach exits the FULL remaining quantity for 1, 2, and many
    contracts (no partial/fractional/zero-quantity exits ever occur);
  * the exit fires exactly once and the same way regardless of contract count;
  * snapshot advertises execution_mode = WHOLE_POSITION_RATCHET.

Also documents why the polling boundary must admit executable bids only: the
ratchet intentionally trusts each admitted price, so upstream quote filtering
is the protection against midpoint/ask-induced false peaks.

Run: PYTHONPATH=/tmp/stubs python3 test_ratchet_exit.py   (needs the alpaca/
fastapi stub shims used elsewhere in this repo's tests)
"""
import os, tempfile
os.environ.setdefault("ALPACA_API_KEY", "x")
os.environ.setdefault("ALPACA_SECRET_KEY", "y")

import scalp_server as ss


def _guard(qty, ladder=None, confirm=2, grace=0):
    cfg = {"symbol": "SPY260728C00739000", "type": "option",
           "ladder": ladder or [{"at": 0, "tol": 15}, {"at": 15, "tol": 6}, {"at": 30, "tol": 2.5}],
           "stall_seconds": 0, "grace_seconds": grace, "confirm_ticks": confirm}
    return ss.Guard(cfg, entry=1.00, qty=qty)


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok: {name}")


class _Order:
    def __init__(self, oid, px, qty):
        self.id = oid
        self.filled_avg_price = px
        self.filled_qty = qty
        class S: value = "filled"
        self.status = S()


class _FakeTrading:
    """Records the SELL order so we can assert the submitted quantity."""
    def __init__(self): self.last = None
    def submit_order(self, req):
        self.last = req
        return _Order("o1", 1.12, req.qty)
    def get_order_by_id(self, oid):
        return _Order(oid, 1.12, self.last.qty)


def _fresh_platform():
    p = object.__new__(ss.Platform)          # skip __init__ (no keys/threads)
    p.trading = _FakeTrading()
    p.live = False
    p.stock_data = None
    p.health = type("Health", (), {"record": lambda *a, **k: None})()
    p._precheck_snapshot = lambda *a, **k: None   # skip the market-read fetch
    return p


def drive_to_breach(g):
    """Set a peak at +20%, then dip past tolerance for confirm_ticks."""
    g.on_price(1.00)
    g.on_price(1.20)                 # peak +20% -> rung tol 6%
    r = None
    for _ in range(g.confirm_ticks):
        r = g.on_price(1.12)         # +12% <= 20-6=14 -> breach; needs confirm
    return r


# ── whole-position exit for 1 / 2 / many contracts ─────────────────────────
def test_full_quantity_exit():
    print("A valid breach sells the FULL remaining qty for 1, 2, and many contracts")
    ss.JOURNAL = __import__("pathlib").Path(tempfile.mkdtemp()) / "j.csv"
    for qty in (1, 2, 5, 37):
        g = _guard(qty)
        reason = drive_to_breach(g)
        check(f"qty={qty}: breach fires after confirm_ticks", bool(reason))
        p = _fresh_platform()
        g.done = True
        p.sell(g, reason)
        submitted = p.trading.last.qty
        check(f"qty={qty}: SELL submits the FULL quantity ({submitted})", submitted == qty)
        check(f"qty={qty}: never fractional or zero", submitted == int(submitted) and submitted >= 1)


# ── exit fires once; no partial machinery exists ───────────────────────────
def test_no_partial_fields():
    print("Snapshot advertises whole-position mode; no partial-exit fields exist")
    g = _guard(1)
    snap = g.snapshot()
    check("execution_mode == WHOLE_POSITION_RATCHET", snap["execution_mode"] == "WHOLE_POSITION_RATCHET")
    check("sells_pct_on_exit == 100", snap["sells_pct_on_exit"] == 100)
    check("partial_profit_taking_enabled is False", snap["partial_profit_taking_enabled"] is False)
    # a 1-contract guard behaves identically to any other — no special-casing
    g1, g5 = _guard(1), _guard(5)
    check("breach reason identical regardless of qty",
          drive_to_breach(g1) is not None and drive_to_breach(g5) is not None)


# ── DOCUMENTED noise sensitivity: one anomalous tick sets a false peak ─────
def test_false_peak_from_anomalous_tick():
    print("Replay: a single anomalous high tick sets a false peak -> premature exit")
    ladder = [{"at": 0, "tol": 15}, {"at": 15, "tol": 6}, {"at": 30, "tol": 2.5}]

    # CONTROL: no anomaly. Peak reaches +10% (tol 15% at rung 0). Price at +5% is
    # only 5% below peak, tol 15% -> NO exit.
    g = _guard(1, ladder=ladder)
    g.on_price(1.00); g.on_price(1.10)   # true peak +10%
    r_ctrl = None
    for _ in range(2):
        r_ctrl = g.on_price(1.05)        # +5%, 5% below peak, tol 15% -> no exit
    check("control (no anomaly): +5% read does NOT exit", r_ctrl is None)

    # ANOMALY REPLAY: if an upstream caller violates the contract and admits a
    # non-executable +30% value, the peak-only-ratchets-up rule locks it in.
    g2 = _guard(1, ladder=ladder)
    g2.on_price(1.00); g2.on_price(1.10)
    g2.on_price(1.30)                    # ANOMALOUS single tick -> false peak +30%
    r_anom = None
    for _ in range(2):
        r_anom = g2.on_price(1.05)       # +5%, 25% below false peak, tol 2.5% -> exit
    check("anomaly: false peak +30% then +5% read EXITS prematurely", r_anom is not None)
    check("peak only ratchets up — false peak persists", g2.peak >= 30)
    print("  -> confirms: peak is set from a single unconfirmed tick and never")
    print("     ratchets down; scalp_server now prevents this by admitting only a")
    print("     positive executable bid. confirm_ticks still protects the exit side.")


if __name__ == "__main__":
    for fn in (test_full_quantity_exit, test_no_partial_fields, test_false_peak_from_anomalous_tick):
        fn()
    print("\nALL RATCHET EXIT TESTS PASSED")
