"""
Wave Riding server helper — READ-ONLY status/report payloads for the gated
endpoints. No orders, no Guard/Platform access, no Standard-mode interaction.
Everything here is a pure read over the feature flag, config, and the shadow
store. When the feature flag is off, the endpoints short-circuit before this
module does anything.
"""
import json
import os
import time

import wave_config as wc
import wave_store as ws
import wave_quotes as wq
import wave_observer as wo
import wave_riding as wr
import wave_scope_policy as scope_policy


def worst_case_exposure(cfg):
    """Configured maximum exposure shown before a user could arm the mode."""
    return {
        "max_total_contracts": cfg.max_total_contracts,
        "estimated_max_total_cost_usd": cfg.max_total_cost_usd,
        "max_configured_risk_usd": cfg.max_position_risk_usd,
    }


def list_positions(base=ws.POSITION_DIR):
    out = []
    if os.path.isdir(base):
        for fn in sorted(os.listdir(base)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(base, fn)) as f:
                    out.append(json.load(f))
            except Exception:
                continue
    return out


def status_payload():
    cfg = wc.default_config()
    positions = list_positions()
    fields = ("position_id", "state", "direction", "open_quantity", "filled_adds",
              "max_adds", "max_total_contracts", "weighted_average_option_cost",
              "total_cost_basis_usd", "current_executable_value_usd",
              "peak_executable_value_usd", "effective_giveback_pct",
              "last_wave_anchor_underlying_price", "frozen_wave_atr",
              "source_position_id")
    return {
        "enabled": True,
        "mode": "WAVE_RIDING",
        "shadow_only": True,
        "live_orders_reachable": False,
        "strategy_version": wc.WAVE_RIDING_VERSION,
        "shadow_scope_version": scope_policy.SCOPE_VERSION,
        "intraday_atr_version": wc.INTRADAY_ATR_VERSION,
        "observer_version": wq.OBSERVER_VERSION,
        "native_index_underlyings": ["SPX"],
        "index_data_source": "alpaca-native-index-values-v1",
        "worst_case_exposure": worst_case_exposure(cfg),
        "positions": [{k: p.get(k) for k in fields} for p in positions],
        "note": ("Shadow-only, EXPERIMENTAL. No live orders. Standard mode and the "
                 "live Guard are unaffected. Thresholds are research parameters, "
                 "not validated trading rules."),
    }


def index_capability(platform):
    """Read-only entitlement/data-density probe for the native SPX adapter."""
    from datetime import datetime, timezone
    try:
        latest = platform.index_data.latest_value("SPX")
        bars = _minute_buffer(platform, "SPX")
        age = max(0.0, datetime.now(timezone.utc).timestamp()
                  - latest["provider_ts_epoch"])
        return {
            "available": True,
            "shadow_only": True,
            "symbol": "SPX",
            "source": latest["source"],
            "latest_provider_timestamp": latest["provider_ts_iso"],
            "latest_age_seconds": round(age, 3),
            "session_minute_bars": len(bars),
            "atr_warmup_ready": len(bars) >= wc.default_config().atr_warmup_min_bars * 5,
            "alignment_reference_type": "sampled_index_twap",
        }
    except Exception as exc:
        message = str(exc)[:240]
        missing_grant = "403" in message and "insufficient grants" in message.lower()
        return {
            "available": False,
            "shadow_only": True,
            "symbol": "SPX",
            "reason_code": ("ALPACA_INDEX_VALUES_GRANT_MISSING" if missing_grant
                            else "INDEX_DATA_UNAVAILABLE"),
            "error": message,
            "action": ("Trader Plus covers SIP/OPRA, but this API identity still lacks "
                       "Alpaca Index Values access. Ask Alpaca Support to enable "
                       "/v1beta1/indices/* for the account/API keys."
                       if missing_grant else "Retry the native index-data check."),
        }


# ── runtime observation building (READ-ONLY market data) ────────────────────

def _session_context(platform, cfg):
    """Session status. market_open is authoritative from the broker clock;
    session_minute/windows are ET wall-clock derived (shadow-grade)."""
    from zoneinfo import ZoneInfo
    from datetime import datetime
    ET = ZoneInfo("America/New_York")
    now = datetime.now(ET)
    mins = now.hour * 60 + now.minute
    open_min, close_min = 9 * 60 + 30, 16 * 60
    try:
        market_open = bool(platform.trading.get_clock().is_open)
    except Exception:
        market_open = now.weekday() < 5 and open_min <= mins < close_min
    smin = max(0, mins - open_min)
    return {"market_open": market_open, "session_minute": smin,
            "in_open_window": market_open and smin < cfg.market_open_delay_minutes,
            "in_close_window": market_open and (close_min - mins) <= cfg.no_new_adds_before_close_minutes}


def _minute_buffer(platform, symbol):
    """Recent regular-session 1-min underlying bars [{t,high,low,close,volume}]
    (IEX). READ-ONLY."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    now_et = datetime.now(ET)
    open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et < open_et:
        return []
    close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if scope_policy.is_native_index(symbol):
        index_data = getattr(platform, "index_data", None)
        if index_data is None:
            raise RuntimeError("Alpaca native index client is not configured")
        end_et = min(now_et, close_et)
        return index_data.minute_bars(
            "SPX", session_open=open_et, now=end_et)

    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    feed = DataFeed.SIP if platform.feed == "sip" else DataFeed.IEX
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                           start=open_et.astimezone(timezone.utc), feed=feed)
    raw = platform.stock_data.get_stock_bars(req).data.get(symbol) or []
    open_utc = open_et.astimezone(timezone.utc)
    bars = []
    for b in raw:
        t = int((b.timestamp - open_utc).total_seconds() // 60)
        if t < 0:
            continue
        bars.append({"t": t, "high": float(b.high), "low": float(b.low),
                     "close": float(b.close), "volume": float(getattr(b, "volume", 0) or 0)})
    bars.sort(key=lambda x: x["t"])
    return bars


def make_observation(platform, pos_like, sequence, cfg):
    """Build ONE synchronized read-only observation for a (simulated) position."""
    from datetime import datetime, timezone
    sess = _session_context(platform, cfg)
    buf = _minute_buffer(platform, pos_like["underlying_symbol"])
    now = datetime.now(timezone.utc)
    return wq.fetch_synchronized(
        stock_data=platform.stock_data, option_data=platform.option_data,
        feed=platform.feed, underlying_symbol=pos_like["underlying_symbol"],
        contract_symbol=pos_like["contract_symbol"], side=pos_like["direction"],
        cfg=cfg, sequence=sequence, minute_buffer=buf, session=sess,
        now_epoch=now.timestamp(), now_iso=now.isoformat(),
        index_data=getattr(platform, "index_data", None),
        underlying_is_index=scope_policy.is_native_index(pos_like["underlying_symbol"]))


# ── lifecycle controls (shadow-only) ────────────────────────────────────────

def start_simulation(platform, spec):
    if not wc.feature_enabled():
        return {"created": False, "blocking_reason": ["FEATURE_DISABLED"]}
    cfg = wc.default_config()
    underlying = scope_policy.validate_underlying(spec.get("underlying_symbol"))
    parsed = scope_policy.validate_contract_direction(
        spec.get("option_contract_symbol"), spec.get("direction"))
    if parsed["underlying"] != underlying:
        raise ValueError(
            f"Contract underlying {parsed['underlying']} does not match {underlying}."
        )
    contract = parsed["symbol"]
    eligible = {row["symbol"] for row in list_contracts(underlying, limit=None)
                .get("contracts", [])}
    if contract not in eligible:
        raise ValueError(
            f"{contract} is not an eligible contract in the latest cached {underlying} Workup."
        )
    pid = spec.get("shadow_position_id") or f"wr-{int(time.time())}"
    direction = str(spec["direction"]).upper()
    pos_like = {"underlying_symbol": underlying,
                "contract_symbol": contract, "direction": direction}
    obs = make_observation(platform, pos_like, 0, cfg)
    obsvr = wo.ShadowObserver(cfg)
    source = dict(spec.get("source") or {})
    source.update({"research_cohort_id": parsed["research_cohort_id"],
                   "research_scope_version": parsed["scope_version"]})
    if underlying == "SPX":
        source.update({
            "underlying_data_source": "alpaca-native-index-values-v1",
            "underlying_alignment_reference": "sampled_index_twap",
            "contract_underlying": parsed.get("contract_underlying"),
        })
    return obsvr.start(pid, underlying, contract, direction, obs, source=source)


def control(platform, position_id, action):
    if not wc.feature_enabled():
        return {"error": "FEATURE_DISABLED"}
    cfg = wc.default_config()
    obsvr = wo.ShadowObserver(cfg)
    pos = ws.load_position(position_id)
    if pos is None:
        return {"error": "unknown_position"}
    if action == "pause":
        obsvr.pause(pos); return {"position_id": position_id, "status": "PAUSED"}
    if action == "resume":
        obsvr.resume(pos); return {"position_id": position_id, "status": "RESUMED"}
    if action == "abandon":
        obsvr.abandon(pos); return {"position_id": position_id, "status": "ABANDONED_WITHOUT_TERMINAL_FILL"}
    if action == "stop":
        obs = make_observation(platform, pos, ws.last_processed_sequence(position_id) + 1, cfg)
        r = obsvr.stop(pos, obs)
        return {"position_id": position_id, "status": r["status"]}
    return {"error": f"unknown_action:{action}"}


def list_contracts(ticker, limit=15):
    """READ-ONLY picker source: read the latest cached workup run for `ticker`
    and return liquid in-band contracts (delta 0.15–0.70) with their OCC symbols,
    sorted most-liquid first. This only prefills the UI — it never selects a
    contract or changes any Wave Riding behavior. If no workup is cached, the
    caller is told to pull one from /workup first."""
    import glob
    import json
    t = scope_policy.validate_underlying(ticker)
    files = sorted(glob.glob(os.path.join("runs", f"{t}_*.json")))
    if not files:
        return {"ticker": t, "contracts": [],
                "note": f"No cached workup for {t}. Open /workup, type {t}, then retry."}
    try:
        payload = json.load(open(files[-1]))
    except Exception:
        return {"ticker": t, "contracts": [], "note": "workup cache unreadable"}
    band = [c for c in payload.get("contracts", [])
            if c.get("delta") is not None and 0.15 <= abs(c["delta"]) <= 0.70
            and c.get("dte") is not None
            and ((t == "SPY" and 0 <= int(c["dte"]) <= 2)
                 or (t == "VOO" and 7 <= int(c["dte"]) <= 60
                     and str(c.get("type") or "").upper() in ("C", "CALL"))
                 or (t == "SPX" and 0 <= int(c["dte"]) <= 60)
                 or (t not in ("SPY", "VOO", "SPX") and 7 <= int(c["dte"]) <= 60))
            and c.get("symbol")]
    band.sort(key=lambda c: (-(c.get("volume") or 0), c.get("spread_pct") or 99))

    def _clean(c):
        # DATA-QUALITY bar only (tight spread + real liquidity) — NOT a profit
        # prediction. These are the contracts most likely to give clean,
        # synchronized quotes the observer won't block.
        sp, oi, vol = c.get("spread_pct"), (c.get("oi") or 0), (c.get("volume") or 0)
        return bool(sp is not None and sp <= 2.0 and oi >= 500 and vol >= 50)

    out = [{"symbol": c.get("symbol"), "expiry": c.get("expiry"), "strike": c.get("strike"),
            "type": c.get("type"), "spread_pct": c.get("spread_pct"), "oi": c.get("oi"),
            "volume": c.get("volume"), "delta": c.get("delta"), "dte": c.get("dte"),
            "bid": c.get("bid"), "ask": c.get("ask"), "mid": c.get("mid"),
            "clean": _clean(c)}
           for c in (band if limit is None else band[:limit])]
    return {"ticker": t, "as_of": payload.get("as_of"),
            "clean_bar": "spread ≤ 2% · OI ≥ 500 · vol ≥ 50 (data quality, not a profit signal)",
            "contracts": out}


def observer_tick(platform):
    """Gated, read-only, exception-isolated per position. Near-zero cost when the
    flag is off or no simulations are active."""
    if not wc.feature_enabled():
        return
    active = ws.list_active()
    if not active:
        return
    cfg = wc.default_config()
    obsvr = wo.ShadowObserver(cfg)
    for pid in active:
        try:
            pos = ws.load_position(pid)
            if pos is None or pos.get("state") in (wr.CLOSED,):
                ws.unregister_active(pid)
                continue
            seq = ws.last_processed_sequence(pid) + 1
            obs = make_observation(platform, pos, seq, cfg)
            obsvr.observe(pos, obs)
        except Exception:
            continue      # per-position isolation; the poll-loop hook logs top-level
