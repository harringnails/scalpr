"""
BAR BUILDER — the audited event→bar path, with an explicit timing contract.

The regime model's math was the easy part; the subtler risk is leakage and
false confidence introduced in the plumbing between a raw market event and a
finalized feature bar. This module makes that plumbing explicit and testable.

Timing contract (enforced, not assumed):
  * Every event carries BOTH provider event time and local receipt time. The
    receipt time is never used as a substitute for event time.
  * Bars are half-open by PROVIDER time: bar k owns [k·B, (k+1)·B). A tick at
    exactly the boundary belongs to the next bar, never to two bars.
  * A bar is finalized only when the watermark (max receipt time seen) reaches
    bar_end + grace — so late/out-of-order arrivals within the grace window are
    still included. After finalization the bar's event set is LOCKED.
  * A late event landing in an already-finalized bar is logged separately and
    counted (including whether it WOULD have changed the features), but never
    silently mutates the finalized bar. Retroactively editing a bar after a
    prediction was issued would create an unreproducible model state.
  * The target for a prediction at bar-close t begins strictly after t. Feature
    interval and target interval never overlap.
  * Each finalized bar carries an input hash over its locked event set, so the
    bar used during live inference can be proven identical to the bar later
    used in validation. Replaying the same raw log reproduces identical hashes.

Order-flow note: the current feed is quote-only (bid/ask + sizes), so imbalance
is computed from resting quote size, not trade classification. The event model
carries a 'trade' type too, so this is ready for a trade feed (SIP/Databento)
without a rewrite — but the trade-vs-quote async hazards are represented in the
test suite now rather than discovered later.
"""

import hashlib
import json
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

BAR_BUILDER_VERSION = "bar-builder-v1"
BAR_SECONDS = 10
NS_PER_S = 1_000_000_000
BAR_NS = BAR_SECONDS * NS_PER_S
FINALIZATION_GRACE_MS = 750
MAX_ACCEPTED_LATENESS_MS = 2000

# Provisional timing-quality gates — retune once real provider behavior is seen.
MAX_LATE_EVENT_RATE = 0.01
MAX_OUT_OF_ORDER_RATE = 0.005
MAX_DUPLICATE_RATE = 0.01
MAX_POST_FINALIZATION_REVISIONS = 0
MAX_P99_DELAY_MS = 2000
# A received time earlier than the provider time is impossible without clock
# skew / a timezone or parsing bug; such events must not silently pull p95/p99
# latency down. A small tolerance absorbs sub-ms jitter.
NEGATIVE_DELAY_TOLERANCE_MS = 1.0
MAX_NEGATIVE_DELAY_RATE = 0.005


@dataclass(frozen=True)
class MarketEvent:
    provider_ts_ns: int          # event time from the provider — the authority for bar ownership
    received_ts_ns: int          # local receipt time — used ONLY for watermark/finalization
    event_type: str              # 'quote' | 'trade'
    price: Optional[float] = None
    size: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    sequence_id: Optional[str] = None


def _bar_index(provider_ts_ns):
    # half-open [k·B, (k+1)·B); floor division puts a boundary tick in the NEXT bar
    return provider_ts_ns // BAR_NS


def _event_fingerprint(e: MarketEvent):
    if e.sequence_id is not None:
        return ("seq", e.sequence_id)
    return (e.provider_ts_ns, e.event_type, e.bid, e.ask, e.price, e.bid_size, e.ask_size)


def _mid(e: MarketEvent):
    if e.event_type == "quote" and e.bid and e.ask:
        return (e.bid + e.ask) / 2
    if e.event_type == "trade" and e.price:
        return e.price
    return None


def _bar_input_hash(events):
    """Deterministic hash over the LOCKED event set of a bar. Same events in any
    arrival order → same hash (events sorted by provider time first)."""
    canon = sorted(
        [(e.provider_ts_ns, e.event_type, e.bid, e.ask, e.price, e.bid_size, e.ask_size)
         for e in events]
    )
    blob = json.dumps(canon, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _bar_features(events):
    """Return (open_mid, close_mid, realized_vol, quote_size_imbalance) from a
    bar's locked events. Imbalance is from resting quote SIZE, not traded flow —
    named accordingly. Return itself is filled in later, index-ordered."""
    mids = [m for m in (_mid(e) for e in sorted(events, key=lambda x: x.provider_ts_ns)) if m]
    if not mids:
        return None
    open_mid, close_mid = mids[0], mids[-1]
    tick_rets = [mids[i] / mids[i - 1] - 1 for i in range(1, len(mids))] if len(mids) > 1 else [0.0]
    vol = float(np.std(tick_rets)) if len(tick_rets) > 1 else 0.0
    flows = [(e.bid_size - e.ask_size) / (e.bid_size + e.ask_size)
             for e in events if e.event_type == "quote"
             and e.bid_size is not None and e.ask_size is not None
             and (e.bid_size + e.ask_size) > 0]
    flow = float(np.mean(flows)) if flows else 0.0
    return open_mid, close_mid, vol, flow


def build_bars_audited(events, grace_ms=FINALIZATION_GRACE_MS, force_finalize=True):
    """Process events in ARRIVAL (receipt-time) order, honoring the timing
    contract above. Returns (bars, quality) where bars is a list of finalized
    bar dicts in ascending bar-index order and quality is the timing report."""
    grace_ns = grace_ms * 1_000_000
    arrival = sorted(events, key=lambda e: e.received_ts_ns)

    open_bars = {}          # idx -> list[MarketEvent] (mutable until finalized)
    locked = {}             # idx -> list[MarketEvent] (frozen event set)
    late_events = []        # events arriving after their bar finalized
    seen = set()
    n_total = 0
    n_dup = 0
    n_ooo = 0
    n_late = 0
    n_revised = 0           # late events that WOULD have changed a finalized bar
    delays_ms = []
    last_provider = None
    watermark = 0

    def finalize_ready(wm):
        for idx in sorted(list(open_bars.keys())):
            bar_end = (idx + 1) * BAR_NS
            if wm >= bar_end + grace_ns:
                locked[idx] = list(open_bars.pop(idx))

    for e in arrival:
        n_total += 1
        watermark = max(watermark, e.received_ts_ns)
        delays_ms.append((e.received_ts_ns - e.provider_ts_ns) / 1e6)

        fp = _event_fingerprint(e)
        if fp in seen:
            n_dup += 1
            continue
        seen.add(fp)

        if last_provider is not None and e.provider_ts_ns < last_provider:
            n_ooo += 1
        last_provider = e.provider_ts_ns

        idx = _bar_index(e.provider_ts_ns)

        # finalize any bars whose grace window has closed as of this watermark
        finalize_ready(watermark)

        if idx in locked:
            # LATE: bar already finalized. Log, check would-have-changed, never mutate.
            n_late += 1
            before = _bar_input_hash(locked[idx])
            after = _bar_input_hash(locked[idx] + [e])
            would_change = before != after
            if would_change:
                n_revised += 1
            late_events.append({"idx": int(idx), "provider_ts_ns": e.provider_ts_ns,
                                "would_have_changed": bool(would_change)})
            continue

        open_bars.setdefault(idx, []).append(e)

    if force_finalize:
        for idx in list(open_bars.keys()):
            locked[idx] = list(open_bars.pop(idx))

    # index-ordered feature pass (returns need the previous bar's close by index)
    bars = []
    prev_close = None
    prev_idx = None
    for idx in sorted(locked.keys()):
        feat = _bar_features(locked[idx])
        if feat is None:
            prev_close, prev_idx = None, idx   # empty/quoteless bar breaks the return chain
            continue
        open_mid, close_mid, vol, flow = feat
        contiguous = prev_idx is not None and idx == prev_idx + 1 and prev_close is not None
        ret = (close_mid / prev_close - 1) if contiguous else 0.0
        bar_start_ns = idx * BAR_NS
        bar_end_ns = (idx + 1) * BAR_NS
        bars.append({
            "bar_index": int(idx),
            "bar_start_ns": int(bar_start_ns), "bar_end_ns": int(bar_end_ns),
            "return": ret, "realized_vol": vol, "quote_size_imbalance": flow,
            "event_count": len(locked[idx]),
            "return_chain_intact": bool(contiguous),
            "input_hash": _bar_input_hash(locked[idx]),
            # target for a prediction AT this bar's close begins strictly after bar_end_ns
            "target_starts_after_ns": int(bar_end_ns),
        })
        prev_close, prev_idx = close_mid, idx

    quality = _quality_report(n_total, n_dup, n_ooo, n_late, n_revised, delays_ms, late_events)
    return bars, quality


def _quality_report(n_total, n_dup, n_ooo, n_late, n_revised, delays_ms, late_events):
    denom = max(n_total, 1)
    delays = np.asarray(delays_ms) if delays_ms else np.array([0.0])
    late_rate = n_late / denom
    ooo_rate = n_ooo / denom
    dup_rate = n_dup / denom
    p95 = float(np.percentile(delays, 95))
    p99 = float(np.percentile(delays, 99))
    min_delay = float(np.min(delays))
    # negative delays = received before provider stamp -> clock skew / tz / parse bug
    n_negative = int(np.sum(delays < -NEGATIVE_DELAY_TOLERANCE_MS))
    negative_delay_rate = n_negative / denom
    clock_skew_suspected = bool(min_delay < -NEGATIVE_DELAY_TOLERANCE_MS)
    passed = (late_rate <= MAX_LATE_EVENT_RATE and ooo_rate <= MAX_OUT_OF_ORDER_RATE
              and dup_rate <= MAX_DUPLICATE_RATE and n_revised <= MAX_POST_FINALIZATION_REVISIONS
              and p99 <= MAX_P99_DELAY_MS and negative_delay_rate <= MAX_NEGATIVE_DELAY_RATE)
    failed_gates = []
    if late_rate > MAX_LATE_EVENT_RATE: failed_gates.append("late_event_rate")
    if ooo_rate > MAX_OUT_OF_ORDER_RATE: failed_gates.append("out_of_order_rate")
    if dup_rate > MAX_DUPLICATE_RATE: failed_gates.append("duplicate_rate")
    if n_revised > MAX_POST_FINALIZATION_REVISIONS: failed_gates.append("post_finalization_revisions")
    if p99 > MAX_P99_DELAY_MS: failed_gates.append("p99_arrival_delay_ms")
    if negative_delay_rate > MAX_NEGATIVE_DELAY_RATE: failed_gates.append("negative_arrival_delay_rate")
    return {
        "events_processed": n_total,
        "late_event_rate": round(late_rate, 5),
        "out_of_order_rate": round(ooo_rate, 5),
        "duplicate_rate": round(dup_rate, 5),
        "min_arrival_delay_ms": round(min_delay, 2),
        "p95_arrival_delay_ms": round(p95, 2),
        "p99_arrival_delay_ms": round(p99, 2),
        "negative_delay_rate": round(negative_delay_rate, 5),
        "clock_skew_suspected": clock_skew_suspected,
        "bars_revised_after_finalization": int(n_revised),
        "late_events_would_have_changed_a_bar": int(n_revised),
        "passed": bool(passed),
        "failed_gates": failed_gates,
    }
