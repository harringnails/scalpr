"""
Wave Riding order adapters. The ONLY place fills happen — the engine stays pure.

`ShadowOrderAdapter` (wave-riding-shadow-fill-v0) is the sole adapter wired in v0.
`LiveBrokerOrderAdapter` is a stub that RAISES — no live order path is reachable.

Conservative shadow fill model:
  * simulated add / entry : contemporaneous ASK + slippage_allowance
  * simulated liquidation : contemporaneous BID − slippage_allowance (floored ≥0)
No fill is generated when the quote is missing, materially stale, crossed,
unusable, too old, or outside the market session. Both the signal timestamp and
the simulated fill timestamp are recorded so latency is visible.
"""
from wave_config import SHADOW_FILL_VERSION


def _quote_ok_for_fill(obs, cfg):
    if not obs.get("market_open"):
        return False, "MARKET_CLOSED"
    if obs.get("quote_quality") in ("crossed", "missing", "unusable", "stale"):
        return False, "QUOTE_INVALID"
    bid, ask = obs.get("option_bid"), obs.get("option_ask")
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return False, "QUOTE_INVALID"
    age = obs.get("quote_age_sec")
    if age is not None and age > cfg.max_quote_age_seconds:
        return False, "QUOTE_TOO_OLD"
    return True, None


class OrderAdapter:
    live = None
    def fill_add(self, obs, qty, cfg): raise NotImplementedError
    def fill_exit(self, obs, qty, cfg): raise NotImplementedError


class ShadowOrderAdapter(OrderAdapter):
    version = SHADOW_FILL_VERSION
    live = False

    def _no_fill(self, obs, why):
        return {"filled": False, "reason": why, "simulated": True,
                "signal_ts": obs.get("signal_ts"), "fill_ts": None,
                "fill_version": self.version}

    def fill_add(self, obs, qty, cfg):
        ok, why = _quote_ok_for_fill(obs, cfg)
        if not ok:
            return self._no_fill(obs, why)
        price = obs["option_ask"] + cfg.slippage_allowance
        return {"filled": True, "price": round(price, 6), "qty": qty,
                "ts": obs["ts"], "signal_ts": obs.get("signal_ts", obs["ts"]),
                "fill_ts": obs["ts"], "basis": "ask_plus_slippage",
                "fill_version": self.version, "simulated": True}

    def fill_exit(self, obs, qty, cfg):
        ok, why = _quote_ok_for_fill(obs, cfg)
        if not ok:
            return self._no_fill(obs, why)
        price = max(0.0, obs["option_bid"] - cfg.slippage_allowance)
        return {"filled": True, "price": round(price, 6), "qty": qty,
                "ts": obs["ts"], "signal_ts": obs.get("signal_ts", obs["ts"]),
                "fill_ts": obs["ts"], "basis": "bid_minus_slippage",
                "fill_version": self.version, "simulated": True}


class LiveBrokerOrderAdapter(OrderAdapter):
    """Deliberately unreachable in v0. Any call raises — no live Wave Riding order
    path exists until a later, separately-approved stage."""
    live = True
    version = "wave-riding-live-DISABLED"

    def fill_add(self, *a, **k):
        raise NotImplementedError("live Wave Riding orders are disabled in wave-riding-v0")

    def fill_exit(self, *a, **k):
        raise NotImplementedError("live Wave Riding orders are disabled in wave-riding-v0")
