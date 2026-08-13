"""
Synthetic timing tests for bar_builder — the 10 enumerated hazard cases plus
replay reproducibility. Run: python3 test_bar_builder.py
No external test framework; plain asserts so it runs anywhere.
"""

import bar_builder as bb
from bar_builder import MarketEvent, BAR_NS, NS_PER_S

S = NS_PER_S


def q(provider_s, received_s=None, bid=100.0, ask=100.02, bs=200, as_=180, seq=None):
    r = provider_s if received_s is None else received_s
    return MarketEvent(int(provider_s * S), int(r * S), "quote",
                       bid=bid, ask=ask, bid_size=bs, ask_size=as_, sequence_id=seq)


def trade(provider_s, received_s=None, price=100.01, size=10, seq=None):
    r = provider_s if received_s is None else received_s
    return MarketEvent(int(provider_s * S), int(r * S), "trade",
                       price=price, size=size, sequence_id=seq)


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok: {name}")


# ── Case 5: tick exactly on the bar boundary belongs to exactly ONE bar ─────
def test_boundary():
    print("Case 5: boundary tick ownership")
    # bar 0 = [0,10s), bar 1 = [10s,20s). A tick at exactly 10.000000s -> bar 1.
    events = [q(5.0), q(9.999), q(10.0), q(15.0)]
    bars, _ = bb.build_bars_audited(events)
    idxs = {b["bar_index"]: b["event_count"] for b in bars}
    check("tick at 9.999s in bar 0", 0 in idxs)
    check("tick at exactly 10.0s in bar 1 not bar 0", idxs.get(1, 0) == 2)  # 10.0 and 15.0
    # no event double-counted: total events across bars == input count
    check("no event enters two bars", sum(b["event_count"] for b in bars) == 4)


# ── Case 6: tick delayed beyond grace -> late, does not mutate finalized bar ─
def test_late_beyond_grace():
    print("Case 6: delayed-beyond-grace event is late, never mutates finalized bar")
    # bar 0 tick, then far-future ticks push watermark past bar0_end+grace,
    # THEN a late bar-0 tick arrives.
    events = [
        q(1.0, received_s=1.0, bid=100.0, ask=100.02),
        q(25.0, received_s=25.0),   # watermark 25s >> 10s + 0.75s -> bar 0 finalizes
        q(2.0, received_s=26.0, bid=999.0, ask=999.02),  # late bar-0 event, big price
    ]
    bars, quality = bb.build_bars_audited(events)
    bar0 = [b for b in bars if b["bar_index"] == 0][0]
    check("late event counted", quality["late_event_rate"] > 0)
    check("late event flagged would-have-changed", quality["bars_revised_after_finalization"] == 1)
    # the finalized bar-0 close must reflect ONLY the on-time event (100.01 mid), not 999
    check("finalized bar not mutated by late event", bar0["event_count"] == 1)


# ── Case 3: duplicate event is dropped ──────────────────────────────────────
def test_duplicate():
    print("Case 3: duplicate provider event dropped")
    e = q(3.0, seq="abc")
    events = [e, q(3.0, seq="abc"), q(4.0, seq="def")]
    bars, quality = bb.build_bars_audited(events)
    check("duplicate counted", quality["duplicate_rate"] > 0)
    check("duplicate not double-counted in bar", sum(b["event_count"] for b in bars) == 2)


# ── Case 4: out-of-order provider sequence is measured ──────────────────────
def test_out_of_order():
    print("Case 4: out-of-order provider sequence measured")
    # received in this order, but provider timestamps go backwards once
    events = [q(5.0, received_s=1.0), q(4.0, received_s=1.1), q(6.0, received_s=1.2)]
    bars, quality = bb.build_bars_audited(events)
    check("out-of-order detected", quality["out_of_order_rate"] > 0)


# ── Cases 1 & 2: trade/quote ordering both land in the right bar ────────────
def test_trade_quote_async():
    print("Cases 1&2: trade-before-quote and quote-before-trade both bar-correct")
    # trade at 2.0 received before the quote at 1.9 (provider) that preceded it
    events = [trade(2.0, received_s=2.05, seq="t1"), q(1.9, received_s=2.06, seq="q1")]
    bars, _ = bb.build_bars_audited(events)
    check("both events land in bar 0 by provider time", bars[0]["event_count"] == 2)
    # reversed arrival order must give the SAME bar assignment (provider time governs)
    events_rev = [q(1.9, received_s=2.06, seq="q1"), trade(2.0, received_s=2.05, seq="t1")]
    bars_rev, _ = bb.build_bars_audited(events_rev)
    check("provider time governs ownership regardless of arrival order",
          bars_rev[0]["event_count"] == 2)


# ── Case 7: empty bar breaks the return chain, does not fabricate a return ──
def test_empty_bar():
    print("Case 7: gap/empty bar breaks the return chain")
    # bar 0 has data, bar 1 empty, bar 2 has data -> bar 2 return must not chain
    events = [q(5.0, bid=100, ask=100.02), q(25.0, bid=101, ask=101.02)]  # bars 0 and 2
    bars, _ = bb.build_bars_audited(events)
    b2 = [b for b in bars if b["bar_index"] == 2][0]
    check("non-contiguous bar has return_chain_intact False", b2["return_chain_intact"] is False)
    check("non-contiguous bar return is 0 not fabricated", b2["return"] == 0.0)


# ── Case 8 & 9: quote-only and trade-only bars both produce features ────────
def test_quote_only_trade_only():
    print("Cases 8&9: quote-only and trade-only bars")
    quote_only = [q(1.0), q(2.0)]
    bars_q, _ = bb.build_bars_audited(quote_only)
    check("quote-only bar builds", len(bars_q) == 1 and bars_q[0]["event_count"] == 2)
    trade_only = [trade(1.0, price=100.0, seq="a"), trade(2.0, price=100.1, seq="b")]
    bars_t, _ = bb.build_bars_audited(trade_only)
    check("trade-only bar builds (mid from trade price)", len(bars_t) == 1)
    check("trade-only bar has zero quote-size imbalance (no quote sizes)",
          bars_t[0]["quote_size_imbalance"] == 0.0)


# ── Case 10 + reproducibility: replay identical log -> identical hashes ─────
def test_replay_reproducibility():
    print("Case 10: connection replay reproduces identical bars/hashes")
    import random
    events = []
    for i in range(200):
        t = 1.0 + i * 0.3
        events.append(q(t, received_s=t + 0.05, bid=100 + i * 0.001, ask=100.02 + i * 0.001,
                        seq=f"s{i}"))
    bars1, _ = bb.build_bars_audited(events)
    # simulate a reconnect/replay: same events, shuffled ARRIVAL order (received order
    # differs) but same provider timestamps -> must reproduce identical finalized bars
    shuffled = events[:]
    random.Random(1).shuffle(shuffled)
    bars2, _ = bb.build_bars_audited(shuffled)
    hashes1 = {b["bar_index"]: b["input_hash"] for b in bars1}
    hashes2 = {b["bar_index"]: b["input_hash"] for b in bars2}
    check("same bar indices on replay", set(hashes1) == set(hashes2))
    check("identical per-bar input hashes on replay", hashes1 == hashes2)


# ── target strictly after feature boundary (no overlap) ─────────────────────
def test_target_strictly_future():
    print("Invariant: target interval starts strictly after feature bar close")
    events = [q(1.0), q(5.0), q(15.0)]
    bars, _ = bb.build_bars_audited(events)
    for b in bars:
        check(f"bar {b['bar_index']} target starts at/after bar_end",
              b["target_starts_after_ns"] == b["bar_end_ns"])


# ── clean stream passes the quality gate ────────────────────────────────────
def test_clean_stream_passes():
    print("Clean stream passes timing-quality gate")
    events = [q(1.0 + i * 0.5, received_s=1.0 + i * 0.5 + 0.02, seq=f"c{i}") for i in range(100)]
    _, quality = bb.build_bars_audited(events)
    check("clean stream passes", quality["passed"] is True)
    check("no revisions on clean stream", quality["bars_revised_after_finalization"] == 0)


def test_forward_log_return_is_cumulative():
    print("v3: forward target is CUMULATIVE over the horizon, not the last bar")
    import numpy as np
    import regime_research as rr
    close = np.array([100.0, 101.0, 102.0, 103.0])
    actual = rr.forward_log_return(close, index=0, horizon=3)
    expected = np.log(103.0 / 100.0)      # whole-horizon move, not log(103/102)
    check("cumulative forward log return over 3 bars", np.isclose(actual, expected))
    check("NOT the isolated final-bar return",
          not np.isclose(actual, np.log(103.0 / 102.0)))
    check("out-of-range returns NaN", np.isnan(rr.forward_log_return(close, 2, 5)))


if __name__ == "__main__":
    for fn in [test_boundary, test_late_beyond_grace, test_duplicate, test_out_of_order,
               test_trade_quote_async, test_empty_bar, test_quote_only_trade_only,
               test_replay_reproducibility, test_target_strictly_future, test_clean_stream_passes,
               test_forward_log_return_is_cumulative]:
        fn()
    print("\nALL BAR-BUILDER TIMING TESTS PASSED")
