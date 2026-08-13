"""
wave_quotes.py — synchronized, READ-ONLY market observation for the shadow
observer. No orders, no Guard/Platform state, no Standard-mode interaction.

Field-level provenance (verified against the live poll loop) is preserved
independently — the underlying and the option come from DIFFERENT feeds and are
NEVER merged into one synthetic market timestamp:

  * underlying latest quote : Alpaca stock latest-quote, IEX feed
  * underlying 5-min bars/ATR: Alpaca stock bars, IEX feed
  * option bid/ask quote     : Alpaca options latest-quote, OPRA-based feed

Simulated option fills are therefore OPRA-derived, not IEX-derived.

`assemble_observation` is pure/testable. `fetch_synchronized` is the thin
client-calling wrapper the observer uses at runtime.
"""
import feature_engine as fe
import wave_atr

OBSERVER_VERSION = "wave-riding-shadow-observer-v0"

SRC_UNDERLYING_QUOTE = "alpaca_stock_latest_quote:{feed}"
SRC_OPTION_QUOTE = "alpaca_options_latest_quote:opra"
SRC_UNDERLYING_BARS = "alpaca_stock_bars:{feed}"
SRC_INDEX_VALUE = "alpaca_native_index_latest_value:v1"
SRC_INDEX_BARS = "alpaca_native_index_values_aggregated_1m:v1"

SYNC_OK = "SYNCHRONIZED"
SYNC_UNSYNC = "OBSERVATION_UNSYNCHRONIZED"


def _age(now_epoch, ts_epoch):
    if now_epoch is None or ts_epoch is None:
        return None
    return round(now_epoch - ts_epoch, 3)


def assemble_observation(*, sequence, now_epoch, now_iso, side, underlying_symbol,
                         contract_symbol, u_quote, o_quote, atr_audit, cfg,
                         session, vwap=None, feed="iex",
                         underlying_quote_source=None, underlying_bars_source=None,
                         alignment_reference_type="session_vwap"):
    """Build one frozen observation with independent field-level sources +
    timestamps + a formal synchronization gate. PURE.

    u_quote / o_quote: {"bid","ask","provider_ts_epoch","provider_ts_iso"}.
    `session`: {"market_open","in_open_window","in_close_window","session_minute"}.
    `atr_audit`: the wave_atr.compute_intraday_atr dict (frozen at wave start by
    the engine; here it carries the CURRENT ATR for freezing decisions)."""
    u_bid, u_ask = u_quote.get("bid"), u_quote.get("ask")
    o_bid, o_ask = o_quote.get("bid"), o_quote.get("ask")
    u_mid = ((u_bid + u_ask) / 2) if (u_bid and u_ask) else (u_bid or u_ask)
    o_mid = ((o_bid + o_ask) / 2) if (o_bid and o_ask) else None
    spread_pct = (round((o_ask - o_bid) / o_mid * 100, 3) if (o_mid and o_bid and o_ask) else None)

    u_ts = u_quote.get("provider_ts_epoch")
    o_ts = o_quote.get("provider_ts_epoch")
    skew = (round(abs(u_ts - o_ts), 3) if (u_ts is not None and o_ts is not None) else None)
    max_skew = cfg.max_underlying_option_timestamp_skew_seconds
    unsync = (skew is None) or (skew > max_skew)
    sync_status = SYNC_UNSYNC if unsync else SYNC_OK

    # option quote-quality (reuse Phase-1 assessor; age vs the OPTION provider ts)
    contract_like = {"bid": o_bid, "ask": o_ask, "mid": o_mid, "spread_pct": spread_pct}
    quality, warning, _age_q = fe.assess_quote_quality(
        contract_like, decision_ts=now_iso, quote_as_of=o_quote.get("provider_ts_iso"))

    above_ok = None
    if vwap is not None and u_mid is not None:
        above_ok = (u_mid > vwap) if side == "CALL" else (u_mid < vwap)

    return {
        "observer_version": OBSERVER_VERSION,
        "observation_sequence": sequence,
        "now": now_epoch, "ts": now_iso,
        "side": side, "underlying_symbol": underlying_symbol, "contract_symbol": contract_symbol,
        # engine-facing fields
        "underlying_price": u_mid,
        "option_bid": o_bid, "option_ask": o_ask, "option_mid": o_mid,
        "option_last": o_mid,
        "spread_pct": spread_pct,
        "quote_age_sec": _age(now_epoch, o_ts),          # execution quote (option)
        "quote_quality": quality, "quote_quality_warning": warning,
        "market_open": session.get("market_open"),
        "in_open_window": session.get("in_open_window"),
        "in_close_window": session.get("in_close_window"),
        "session_minute": session.get("session_minute"),
        "above_vwap_side_ok": above_ok, "trend_ok": above_ok,
        "atr_value": atr_audit.get("atr_value"),
        "atr_quality": atr_audit.get("atr_quality"),
        "hard_veto": False,
        "unsynchronized": unsync,
        # independent provenance + timing (never merged)
        "synchronization_status": sync_status,
        "timestamp_skew_seconds": skew,
        "max_underlying_option_timestamp_skew_seconds": max_skew,
        "underlying_quote_timestamp": u_quote.get("provider_ts_iso"),
        "option_quote_timestamp": o_quote.get("provider_ts_iso"),
        "underlying_quote_age_seconds": _age(now_epoch, u_ts),
        "option_quote_age_seconds": _age(now_epoch, o_ts),
        "vwap": vwap,
        "alignment_reference_type": alignment_reference_type,
        "sources": {
            "underlying_quote": (underlying_quote_source
                                 or SRC_UNDERLYING_QUOTE.format(feed=feed)),
            "underlying_5m_bars": (underlying_bars_source
                                   or SRC_UNDERLYING_BARS.format(feed=feed)),
            "option_quote": SRC_OPTION_QUOTE,
            "timestamps": "per_field_provider_timestamps",
            "atr": atr_audit.get("atr_version"),
        },
        "atr_audit": atr_audit,
    }


# ── rolling 5-min ATR + simple session VWAP from the underlying minute buffer ─

def atr_from_buffer(minute_bars, now_minute, cfg):
    return wave_atr.atr_from_minute_bars(
        minute_bars, now_minute=now_minute, period=cfg.intraday_atr_period,
        warmup_min=cfg.atr_warmup_min_bars, full=cfg.atr_full_bars)


def session_vwap(minute_bars):
    num = den = 0.0
    for b in minute_bars:
        tp = (b["high"] + b["low"] + b["close"]) / 3
        num += tp * b.get("volume", 0)
        den += b.get("volume", 0)
    return (num / den) if den else None


# ── runtime wrapper: read-only fetch from the live data clients ─────────────

def fetch_synchronized(*, stock_data, option_data, feed, underlying_symbol,
                       contract_symbol, side, cfg, sequence, minute_buffer,
                       session, now_epoch, now_iso, index_data=None,
                       underlying_is_index=False):
    """READ-ONLY. Calls the same latest-quote endpoints the poll loop uses and
    assembles one synchronized observation. Never touches guards/orders. Raises
    on a data error (the observer isolates it)."""
    from alpaca.data.requests import StockLatestQuoteRequest, OptionLatestQuoteRequest
    from alpaca.data.enums import DataFeed
    feed_enum = DataFeed.SIP if feed == "sip" else DataFeed.IEX

    if underlying_is_index:
        if index_data is None:
            raise ValueError("native index client is unavailable")
        latest = index_data.latest_value(underlying_symbol)
        u_quote = {"bid": latest["value"], "ask": latest["value"],
                   "provider_ts_epoch": latest["provider_ts_epoch"],
                   "provider_ts_iso": latest["provider_ts_iso"]}
        underlying_quote_source = SRC_INDEX_VALUE
        underlying_bars_source = SRC_INDEX_BARS
        alignment_reference_type = "sampled_index_twap"
    else:
        uq = stock_data.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=underlying_symbol, feed=feed_enum))
        q = uq.get(underlying_symbol)
        u_quote = _q_to_dict(q)
        underlying_quote_source = None
        underlying_bars_source = None
        alignment_reference_type = "session_vwap"

    oq = option_data.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=contract_symbol))
    oqv = oq.get(contract_symbol)
    o_quote = _q_to_dict(oqv)

    now_min = session.get("session_minute")
    atr_audit = atr_from_buffer(minute_buffer, now_min, cfg)
    vwap = session_vwap(minute_buffer) if minute_buffer else None
    return assemble_observation(
        sequence=sequence, now_epoch=now_epoch, now_iso=now_iso, side=side,
        underlying_symbol=underlying_symbol, contract_symbol=contract_symbol,
        u_quote=u_quote, o_quote=o_quote, atr_audit=atr_audit, cfg=cfg,
        session=session, vwap=vwap, feed=feed,
        underlying_quote_source=underlying_quote_source,
        underlying_bars_source=underlying_bars_source,
        alignment_reference_type=alignment_reference_type)


def _q_to_dict(q):
    if q is None:
        return {"bid": None, "ask": None, "provider_ts_epoch": None, "provider_ts_iso": None}
    ts = getattr(q, "timestamp", None)
    return {"bid": float(q.bid_price) if q.bid_price else None,
            "ask": float(q.ask_price) if q.ask_price else None,
            "provider_ts_epoch": ts.timestamp() if ts is not None else None,
            "provider_ts_iso": ts.isoformat() if ts is not None else None}
