"""Paper-only Scalpr climb-add controller using Wave Riding's pure add gates.

This is a new, versioned execution policy. It does not change the frozen Wave
Riding v0 study and contains no broker or file I/O.
"""

from dataclasses import dataclass
import os

import scope_policy
from wave_config import WaveConfig
from wave_riding import WaveEngine, idempotency_key, new_position

VERSION = "scalpr-climb-add-paper-v0"
FEATURE_FLAG_ENV = "SCALPR_CLIMB_ADDS_ENABLED"


def feature_enabled():
    return os.getenv(FEATURE_FLAG_ENV, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ClimbConfig:
    enabled: bool = False
    add_contracts: int = 1
    max_adds: int = 2
    max_total_contracts: int | None = None
    trigger_atr_fraction: float = 0.40
    minimum_option_gain_pct: float = 10.0
    confirmation_seconds: float = 10.0
    cooldown_seconds: float = 30.0
    max_total_cost_usd: float = 4500.0
    poll_seconds: float = 5.0

    @classmethod
    def from_dict(cls, raw, initial_qty):
        raw = raw or {}
        allowed = set(cls.__dataclass_fields__)
        cfg = cls(**{k: raw[k] for k in raw if k in allowed})
        cfg.add_contracts, cfg.max_adds = int(cfg.add_contracts), int(cfg.max_adds)
        cfg.max_total_contracts = int(
            cfg.max_total_contracts or (int(initial_qty) + cfg.add_contracts * cfg.max_adds))
        if cfg.add_contracts < 1 or cfg.max_adds < 1:
            raise ValueError("climb quantities and max adds must be positive whole numbers")
        if cfg.max_total_contracts < int(initial_qty):
            raise ValueError("climb max total contracts cannot be below opening quantity")
        if not 0.05 <= cfg.trigger_atr_fraction <= 2.0:
            raise ValueError("climb ATR fraction must be between 0.05 and 2.0")
        if cfg.minimum_option_gain_pct < 0 or cfg.confirmation_seconds < 0:
            raise ValueError("climb gain and confirmation thresholds cannot be negative")
        if cfg.max_total_cost_usd <= 0 or cfg.poll_seconds < 2.0:
            raise ValueError("climb cost cap must be positive and polling at least 2 seconds")
        return cfg


def wave_config(cfg, initial_qty):
    """Map the Scalpr policy onto the already-tested Wave add gate."""
    return WaveConfig(
        enabled=True, initial_contracts=int(initial_qty),
        add_contracts_per_wave=cfg.add_contracts, max_adds=cfg.max_adds,
        max_total_contracts=cfg.max_total_contracts,
        add_trigger_atr_fraction=cfg.trigger_atr_fraction,
        minimum_option_gain_for_add_pct=cfg.minimum_option_gain_pct,
        confirmation_seconds=cfg.confirmation_seconds,
        add_cooldown_seconds=cfg.cooldown_seconds,
        max_total_cost_usd=cfg.max_total_cost_usd,
        max_incremental_cost_usd=cfg.max_total_cost_usd,
        max_position_risk_usd=cfg.max_total_cost_usd,
        max_portfolio_wave_risk_usd=cfg.max_total_cost_usd,
        shadow_mode=True, live_add_orders_enabled=False,
    ).validate()


def initialize(symbol, entry, qty, obs, raw_config=None):
    parsed = scope_policy.validate_option(symbol)
    cfg = ClimbConfig.from_dict(raw_config, qty)
    wc = wave_config(cfg, qty)
    direction = "CALL" if parsed["right"] == "C" else "PUT"
    pos = new_position(f"climb-{symbol}", direction, parsed["underlying"], symbol, wc)
    pos = WaveEngine(wc).seed_initial_fill(
        pos, {"price": float(entry), "qty": int(qty), "ts": obs["ts"]}, obs)
    return {"version": VERSION, "config": cfg.__dict__.copy(), "position": pos,
            "status": "MONITORING", "reason_codes": ["INITIAL_OBSERVATION_FROZEN"],
            "confirmation_started_at": None, "pending": False,
            "last_observation_at": obs["ts"]}


def evaluate(state, obs):
    """Return copied state plus an ADD intent only after persistent eligibility."""
    out = {**state, "position": dict(state["position"]), "last_observation_at": obs["ts"]}
    initial_qty = out["position"]["initial_quantity"]
    cfg = ClimbConfig.from_dict(out["config"], initial_qty)
    if (out["position"]["filled_adds"] >= cfg.max_adds
            or out["position"]["open_quantity"] >= cfg.max_total_contracts):
        out.update(status="MAX_POSITION_REACHED", reason_codes=["MAX_ADDS_REACHED"],
                   confirmation_started_at=None)
        return out, None
    eligible, reasons, metrics = WaveEngine(wave_config(cfg, initial_qty)).evaluate_add(
        out["position"], obs)
    out["reason_codes"], out["metrics"] = reasons, metrics
    if not eligible:
        out.update(status="MONITORING", confirmation_started_at=None)
        return out, None
    started = out.get("confirmation_started_at")
    if started is None:
        out.update(status="CONFIRMING", confirmation_started_at=obs["now"])
        return out, None
    if obs["now"] - started < cfg.confirmation_seconds:
        out["status"] = "CONFIRMING"
        return out, None
    key = idempotency_key(out["position"], cfg.add_contracts, obs["ts"])
    out.update(status="ADD_PENDING", pending=True)
    out["position"]["pending_order_key"] = key
    return out, {"idempotency_key": key, "qty": cfg.add_contracts,
                 "limit_price": obs["option_ask"]}


def apply_fill(state, fill, obs):
    out = {**state, "position": dict(state["position"])}
    initial_qty = out["position"]["initial_quantity"]
    cfg = ClimbConfig.from_dict(out["config"], initial_qty)
    out["position"] = WaveEngine(wave_config(cfg, initial_qty)).apply_add_fill(
        out["position"], fill, obs)
    out.update(status=out["position"]["state"], pending=False,
               confirmation_started_at=None, reason_codes=["BROKER_FILL_RECONCILED"])
    return out


def reject(state, reason):
    out = {**state, "position": dict(state["position"])}
    out["position"]["pending_order_key"] = None
    out.update(status="MONITORING", pending=False, confirmation_started_at=None,
               reason_codes=[reason])
    return out
