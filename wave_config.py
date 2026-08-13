"""
Wave Riding v0 — versioned configuration + feature flag. SHADOW ONLY.

This module holds the versioned `WaveConfig` for the Wave Riding shadow simulator.
It never places orders and never touches Standard mode. Validation enforces the
non-negotiable v0 boundaries: averaging-down is impossible, shadow mode is forced,
and no live order path is enabled.

Version register (documented as EXPERIMENTAL research parameters, NOT validated
trading rules):
  * wave-riding-v0             — the engine / state machine / position record
  * intraday-atr-v0            — the 5-minute intraday ATR service
  * wave-riding-shadow-fill-v0 — the conservative shadow fill model
  * wave-riding-baseline-v0    — the three-track comparison harness
"""
from dataclasses import dataclass, asdict, fields
import os

WAVE_RIDING_VERSION = "wave-riding-v0"
INTRADAY_ATR_VERSION = "intraday-atr-v0"
SHADOW_FILL_VERSION = "wave-riding-shadow-fill-v0"
BASELINE_VERSION = "wave-riding-baseline-v0"

FEATURE_FLAG_ENV = "WAVE_RIDING_ENABLED"


def feature_enabled():
    """Master feature flag. Default OFF: when off, no Wave Riding state is created,
    no observations are written, no UI controls appear, and Standard mode is
    completely unaffected."""
    return os.environ.get(FEATURE_FLAG_ENV, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class WaveConfig:
    strategy_version: str = WAVE_RIDING_VERSION
    intraday_atr_version: str = INTRADAY_ATR_VERSION
    shadow_fill_version: str = SHADOW_FILL_VERSION
    baseline_version: str = BASELINE_VERSION

    enabled: bool = False
    shadow_mode: bool = True                 # forced true in v0
    live_add_orders_enabled: bool = False    # forced false in v0

    # sizing (whole contracts only)
    initial_contracts: int = 1
    add_contracts_per_wave: int = 1
    max_adds: int = 2
    max_total_contracts: int = 3

    # add trigger — ATR_FRACTION on a FROZEN intraday 5-min ATR (intraday-atr-v0)
    add_trigger_type: str = "ATR_FRACTION"   # ABSOLUTE_POINTS | PERCENTAGE | ATR_FRACTION
    add_trigger_atr_fraction: float = 0.40
    intraday_atr_timeframe_min: int = 5
    intraday_atr_period: int = 14
    atr_bars_source: str = "regular_session"  # or "include_premarket"
    atr_warmup_min_bars: int = 5
    atr_full_bars: int = 14

    # confirmation
    minimum_option_gain_for_add_pct: float = 10.0
    require_position_profitable: bool = True
    require_vwap_alignment: bool = True
    confirmation_mode: str = "PERSISTENCE_SECONDS"  # CONSECUTIVE_OBSERVATIONS | PERSISTENCE_SECONDS | COMPLETED_BAR
    confirmation_seconds: float = 10.0
    add_cooldown_seconds: float = 30.0

    # reversal exit (spread-adjusted; vol floor stubbed/disabled)
    configured_giveback_pct: float = 2.5
    spread_noise_multiplier: float = 1.5
    volatility_noise_floor_pct: float = None   # STUBBED — not fabricated
    peak_confirmation_observations: int = 1    # 1 = off; >1 = require N-persist peak

    # quote quality
    max_spread_pct: float = 5.0
    max_quote_age_seconds: float = 5.0
    # observer-v0: underlying(IEX) vs option(OPRA) timestamp skew gate
    max_underlying_option_timestamp_skew_seconds: float = 2.0

    # exposure / risk caps (hard)
    max_total_cost_usd: float = 4500.0
    max_incremental_cost_usd: float = 4500.0
    max_position_risk_usd: float = 4500.0
    max_portfolio_wave_risk_usd: float = 4500.0

    # profit-funded add gate
    require_profit_funded_add: bool = True
    required_profit_coverage_ratio: float = 0.50

    # invariants
    allow_averaging_down: bool = False         # NON-NEGOTIABLE — must stay false
    suspend_standard_profit_ratchet: bool = True   # SIMULATION-ONLY (never live in v0)
    limit_order_only_for_adds: bool = True

    # session windows
    market_open_delay_minutes: int = 5
    no_new_adds_before_close_minutes: int = 15

    # shadow fill
    slippage_allowance: float = 0.02           # per-share $ added(add)/subtracted(exit)

    # failure handling
    data_outage_suspend_seconds: float = 60.0

    def validate(self):
        errs = []
        if self.allow_averaging_down:
            errs.append("allow_averaging_down must be false (non-negotiable in v0)")
        if not self.shadow_mode:
            errs.append("shadow_mode must be true in v0")
        if self.live_add_orders_enabled:
            errs.append("live_add_orders_enabled must be false in v0")
        for cap in ("max_adds", "max_total_contracts", "max_total_cost_usd",
                    "max_incremental_cost_usd", "max_position_risk_usd"):
            v = getattr(self, cap)
            if v is None or v <= 0:
                errs.append(f"{cap} must be a positive cap")
        if self.max_total_contracts < self.initial_contracts:
            errs.append("max_total_contracts < initial_contracts")
        if self.initial_contracts < 1 or self.add_contracts_per_wave < 1:
            errs.append("whole-contract quantities must be >= 1")
        if self.add_trigger_type not in ("ABSOLUTE_POINTS", "PERCENTAGE", "ATR_FRACTION"):
            errs.append(f"bad add_trigger_type {self.add_trigger_type}")
        if self.required_profit_coverage_ratio < 0:
            errs.append("required_profit_coverage_ratio must be >= 0")
        if errs:
            raise ValueError("WaveConfig invalid: " + "; ".join(errs))
        return self

    @classmethod
    def from_dict(cls, d):
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in allowed}).validate()

    def to_dict(self):
        return asdict(self)


def default_config():
    return WaveConfig().validate()
