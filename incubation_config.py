"""
ENTRY_INCUBATION_SHADOW — versioned configuration + feature flag. SHADOW ONLY.

Capture is variant-agnostic (one canonical option path per trade); the activation
× hard-stop factorial is replayed OFFLINE at finalize. Nothing here changes the
live Guard, exits, orders, or Standard mode.

VERIFIED against the code (do not assume from discussion): the live Guard has NO
separate hard stop — its only protection is the ratchet ladder. So
CURRENT_CONFIGURED_HARD_STOP is registered as None; the de facto floor is the
initial-rung 15% giveback (grace/confirm-gated, ratcheting), which is NOT a true
hard stop.
"""
from dataclasses import dataclass, field, asdict, fields
import os

import feature_engine as fe

INCUBATION_STUDY_VERSION = "standard-entry-incubation-study-v0"
INCUBATION_SHADOW_VERSION = "entry-incubation-shadow-v0"
HARD_STOP_DIMENSION_VERSION = "incubation-hard-stop-dimension-v0"
# Record schema — bumped to v0.1 to add the explicit study-role fields
# (study_role / cohort_eligible / exclusion_reason). A schema change re-locks the
# hash; we never silently retain the old one.
RECORD_SCHEMA_VERSION = "incubation-record-v0.1-study-role"
COHORT_ID = "standard-entry-incubation-shadow-cohort-a"

FEATURE_FLAG_ENV = "INCUBATION_SHADOW_ENABLED"


def enabled():
    return os.environ.get(FEATURE_FLAG_ENV, "").strip().lower() in ("1", "true", "yes", "on")


# Pre-registered hard-stop scenarios (offline replay dimension). CURRENT_CONFIGURED
# is None because the live Guard has no separate hard stop (verified). All others
# are full simulated exits at the given EXECUTABLE-BID return, active throughout,
# subordinate only to simulated emergency/data-safety exits.
HARD_STOP_SCENARIOS = {
    "CURRENT_CONFIGURED_HARD_STOP": None,     # live truth: no separate hard stop
    "HARD_STOP_15": 15.0,
    "HARD_STOP_12_5": 12.5,
    "HARD_STOP_10": 10.0,
}

# The eight pre-registered activation policies (defined in entry_incubation_study).
ACTIVATION_POLICIES = ["CURRENT", "TIME_DELAY_5M", "TIME_DELAY_10M", "PROFIT_BUFFER_5",
                       "PROFIT_BUFFER_10", "TIME_OR_PROFIT", "MOMENTUM_CONFIRMED",
                       "INCUBATION_WITH_HARD_STOP"]

# Live ladder in effect (matches the journal's tol 15/6/2.5). Used to replay the
# Standard ratchet after activation. Mirrors scalp_server.Guard; replay only.
STANDARD_LADDER = [{"at": 0, "tol": 15}, {"at": 15, "tol": 6}, {"at": 30, "tol": 2.5}]
STANDARD_GRACE_SEC = 60.0
STANDARD_CONFIRM_TICKS = 2


@dataclass
class IncubationConfig:
    shadow_version: str = INCUBATION_SHADOW_VERSION
    study_version: str = INCUBATION_STUDY_VERSION
    hard_stop_dimension_version: str = HARD_STOP_DIMENSION_VERSION
    cohort_id: str = COHORT_ID

    # capture
    poll_cadence_hint_s: float = 3.0
    max_underlying_option_timestamp_skew_seconds: float = 2.0
    max_quote_age_seconds: float = 5.0

    # recovery window (post-live-exit), versioned
    recovery_window_minutes: int = 15
    stop_at_market_close: bool = True
    stop_at_expiration: bool = True
    data_quality_failure_limit: int = 20        # consecutive bad/stale obs → terminal

    # replay dimensions (frozen for the cohort)
    activation_policies: tuple = tuple(ACTIVATION_POLICIES)
    hard_stop_scenarios: dict = field(default_factory=lambda: dict(HARD_STOP_SCENARIOS))
    ladder: tuple = field(default_factory=lambda: tuple(tuple(sorted(r.items())) for r in STANDARD_LADDER))
    grace_seconds: float = STANDARD_GRACE_SEC
    confirm_ticks: int = STANDARD_CONFIRM_TICKS
    slippage_allowance: float = 0.02

    # ── materiality (APPROVED + LOCKED). All materiality is computed on the
    #    EXECUTABLE OPTION BID (never mid/last) — enforced by the study engine,
    #    which measures returns off path bids. ──
    materiality_basis: str = "executable_option_bid"
    recovery_materiality_pp: float = 10.0       # "materially higher executable value"
    loose_materiality_pp: float = 10.0          # "materially larger loss"
    qualifying_recovery_window_min: int = 15    # recovery must occur within this window
    materiality_registered: bool = True         # APPROVED

    # ── primary designations (APPROVED) ──
    # NOTE: the live 15% is a giveback from the EVOLVING PEAK, not a fixed
    # entry-based stop. So HARD_STOP_15 (a fixed −15% from entry) is a DIFFERENT
    # mechanism and is NOT equivalent to the existing 15% peak-giveback tier.
    primary_baseline_activation: str = "CURRENT"
    primary_baseline_hard_stop: str = "CURRENT_CONFIGURED_HARD_STOP"     # None (live truth)
    # Clean incubation-effect test: delayed activation, no fixed hard stop — holds
    # the ABSENCE of a separate fixed stop constant with the baseline.
    primary_candidate_activation: str = "TIME_OR_PROFIT"
    primary_candidate_hard_stop: str = "CURRENT_CONFIGURED_HARD_STOP"    # None
    # Separately-reported confirmatory overlay: does a FIXED −15% stop control
    # incubation downside without severing too many recoverable winners?
    primary_safety_overlay_activation: str = "TIME_OR_PROFIT"
    primary_safety_overlay_hard_stop: str = "HARD_STOP_15"
    primary_registered: bool = True             # APPROVED

    def frozen_dimensions(self):
        """The subset that, once approved, is hashed to lock the cohort. Cohort A
        must NOT begin until materiality_registered and primary_registered are True
        (set only by explicit approval)."""
        return {
            "study_version": self.study_version,
            "shadow_version": self.shadow_version,
            "record_schema_version": RECORD_SCHEMA_VERSION,
            "hard_stop_dimension_version": self.hard_stop_dimension_version,
            "activation_policies": list(self.activation_policies),
            "hard_stop_scenarios": self.hard_stop_scenarios,
            "ladder": [dict(r) for r in [dict(x) for x in self.ladder]],
            "grace_seconds": self.grace_seconds, "confirm_ticks": self.confirm_ticks,
            "slippage_allowance": self.slippage_allowance,
            "recovery_window_minutes": self.recovery_window_minutes,
            "materiality_basis": self.materiality_basis,
            "recovery_materiality_pp": self.recovery_materiality_pp,
            "loose_materiality_pp": self.loose_materiality_pp,
            "qualifying_recovery_window_min": self.qualifying_recovery_window_min,
            "primary_baseline": [self.primary_baseline_activation, self.primary_baseline_hard_stop],
            "primary_candidate": [self.primary_candidate_activation, self.primary_candidate_hard_stop],
            "primary_safety_overlay": [self.primary_safety_overlay_activation, self.primary_safety_overlay_hard_stop],
        }

    def config_hash(self):
        return fe.canonical_hash(self.frozen_dimensions())

    def cohort_ready(self):
        """Cohort A may begin only after BOTH the materiality thresholds and the
        primary designations are explicitly approved."""
        return bool(self.materiality_registered and self.primary_registered)

    def to_dict(self):
        return asdict(self)


def default_config():
    return IncubationConfig()
