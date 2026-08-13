"""Pure Entry Intelligence v1 contracts and mechanical reversal evaluation.

Paper/shadow research only.  This module has no broker, server, Guard, or order
imports.  It turns completed regular-session bars plus an explicit point-in-time
option universe into an auditable decision packet.  The three evidence axes stay
separate and may be unavailable; no aggregate or probability is emitted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

import feature_engine as fe
import wave_atr


DECISION_SCHEMA_VERSION = "entry-intelligence-decision-v1"
RULES_VERSION = "entry-reversal-rules-v1-draft"
EPISODE_VERSION = "entry-reversal-episode-v1"
CONTRACT_SELECTION_VERSION = "entry-execution-contract-selection-v2"
FORMAL_COHORT_ELIGIBLE = False
DataState = Literal["FRESH", "STALE", "MISSING", "UNAVAILABLE", "UNUSABLE"]


class EvidenceAxis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    status: DataState
    score_version: str = RULES_VERSION
    passed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)
    is_calibrated_probability: Literal[False] = False

    @model_validator(mode="after")
    def unavailable_has_no_value(self):
        if self.status != "FRESH" and self.value is not None:
            raise ValueError("a non-fresh evidence axis cannot carry a value")
        return self


class EvidenceScoreBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    direction: EvidenceAxis
    quality: EvidenceAxis
    executability: EvidenceAxis
    is_calibrated_probability: Literal[False] = False


class ContractRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    option_symbol: str
    option_type: Literal["CALL", "PUT"]
    expiry: str
    dte: int = Field(ge=0)
    strike: float = Field(gt=0)
    delta: float
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    spread_pct: float = Field(ge=0)
    volume: int = Field(ge=0)
    open_interest: int = Field(ge=0)
    quote_observed_at: datetime
    quote_received_at: datetime
    quote_source: str = "alpaca_options_latest_quote:opra"
    contract_selection_version: str = CONTRACT_SELECTION_VERSION
    contract_selection_hash: Optional[str] = None
    contract_assembly_version: Optional[str] = None
    contract_assembly_hash: Optional[str] = None
    delta_source: Optional[str] = None
    field_provenance: dict[str, Any] = Field(default_factory=dict)


class LevelRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument: Literal["OPTION_BID"] = "OPTION_BID"
    price: float = Field(gt=0)
    rule: str


class DecisionPacket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["entry-intelligence-decision-v1"] = DECISION_SCHEMA_VERSION
    formal_cohort_eligible: Literal[False] = FORMAL_COHORT_ELIGIBLE
    collection_role: Literal["PRELOCK_DRY_RUN"] = "PRELOCK_DRY_RUN"
    decision_id: str
    cohort_id: str
    cohort_hash: Optional[str] = None
    cohort_lock_status: Literal["DRAFT_NOT_LOCKED"] = "DRAFT_NOT_LOCKED"
    rules_version: str = RULES_VERSION
    rules_hash: str
    label_version: str
    episode_version: str = EPISODE_VERSION
    config_version: str
    config_hash: str
    observed_at: datetime
    decided_at: datetime
    symbol: str
    setup_type: str
    decision: Literal["CALL", "PUT", "NO_TRADE"] = "NO_TRADE"
    scores: EvidenceScoreBlock
    selected_contract: Optional[ContractRef] = None
    frozen_target: Optional[LevelRef] = None
    frozen_invalidation: Optional[LevelRef] = None
    supporting_evidence: list[dict[str, Any]] = Field(default_factory=list)
    opposing_evidence: list[dict[str, Any]] = Field(default_factory=list)
    missing_or_stale: list[str] = Field(default_factory=list)
    raw_evidence_refs: list[str] = Field(default_factory=list)
    episode_key: Optional[str] = None

    @model_validator(mode="after")
    def decision_shape(self):
        if self.decided_at < self.observed_at:
            raise ValueError("decided_at precedes observed_at")
        if self.decision == "NO_TRADE":
            if not self.missing_or_stale and not any(
                axis.failed for axis in (
                    self.scores.direction,
                    self.scores.quality,
                    self.scores.executability,
                )
            ):
                raise ValueError("NO_TRADE requires a missing/stale or failed reason")
        elif not (self.selected_contract and self.frozen_target and self.frozen_invalidation):
            raise ValueError("CALL/PUT requires contract, target, and invalidation")
        return self


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def completed_five_minute_bars(minute_bars: list[dict], now_minute: int) -> list[dict]:
    """Completed RTH 5-minute OHLC bars; the forming bucket is excluded."""
    buckets: dict[int, dict] = {}
    for raw in sorted(minute_bars or [], key=lambda row: int(row["t"])):
        minute = int(raw["t"])
        if minute < 0:
            continue
        bucket = minute // 5
        row = buckets.get(bucket)
        if row is None:
            buckets[bucket] = {
                "bucket": bucket,
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
                "volume": float(raw.get("volume") or 0),
                "last_minute": minute,
            }
        else:
            row["high"] = max(row["high"], float(raw["high"]))
            row["low"] = min(row["low"], float(raw["low"]))
            row["volume"] += float(raw.get("volume") or 0)
            if minute >= row["last_minute"]:
                row["close"] = float(raw["close"])
                row["last_minute"] = minute
    forming = int(now_minute) // 5
    return [buckets[key] for key in sorted(buckets) if key < forming]


def wilder_rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    """Wilder RSI aligned with closes; insufficient prefixes are None."""
    out: list[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum(max(v, 0.0) for v in changes[:period]) / period
    loss = sum(max(-v, 0.0) for v in changes[:period]) / period

    def value(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[period] = value(gain, loss)
    for close_index in range(period + 1, len(closes)):
        change = changes[close_index - 1]
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period
        out[close_index] = value(gain, loss)
    return out


def session_vwap(minute_bars: list[dict]) -> Optional[float]:
    num = den = 0.0
    for bar in minute_bars or []:
        volume = float(bar.get("volume") or 0)
        typical = (float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 3
        num += typical * volume
        den += volume
    return num / den if den else None


def _round_level(price: float, increment: float, side: str) -> float:
    units = price / increment
    if side == "CALL":
        return int(units) * increment
    integer = int(units)
    return (integer if units == integer else integer + 1) * increment


def _support_candidates(side: str, context: dict, price: float, increment: float) -> list[tuple[str, float]]:
    if side == "CALL":
        raw = [
            ("prior_day_low", context.get("prior_day_low")),
            ("premarket_low", context.get("premarket_low")),
            ("round_number", _round_level(price, increment, side)),
        ]
    else:
        raw = [
            ("prior_day_high", context.get("prior_day_high")),
            ("premarket_high", context.get("premarket_high")),
            ("round_number", _round_level(price, increment, side)),
        ]
    return [(name, float(level)) for name, level in raw if level is not None and float(level) > 0]


def evaluate_reversal_setup(
    *, symbol: str, side: Literal["CALL", "PUT"], minute_bars: list[dict],
    now_minute: int, context: dict, rules: dict,
) -> dict:
    """Evaluate the fully mechanical draft setup on completed data only.

    The return is evidence, not a decision packet; contract execution is assessed
    separately because missing option data must never be converted to a zero.
    """
    bars = completed_five_minute_bars(minute_bars, now_minute)
    required_bars = max(
        int(rules["atr_period"]), int(rules["rsi_period"]) + 1,
        int(rules["extension_lookback_bars"]) + 1, 3,
    )
    missing = []
    if len(bars) < required_bars:
        missing.append(f"completed_5m_bars<{required_bars}")
    if missing:
        return {
            "symbol": symbol.upper(), "side": side, "status": "MISSING",
            "passed": [], "failed": [], "missing": missing,
            "reference_extreme": None, "reference_level": None,
        }

    current, previous, before = bars[-1], bars[-2], bars[-3]
    atr_audit = wave_atr.compute_intraday_atr(
        [{"high": b["high"], "low": b["low"], "close": b["close"]} for b in bars],
        period=int(rules["atr_period"]), warmup_min=int(rules["atr_period"]),
        full=int(rules["atr_period"]), as_of=current["bucket"],
    )
    atr = atr_audit.get("atr_value")
    if atr is None:
        return {
            "symbol": symbol.upper(), "side": side, "status": "MISSING",
            "passed": [], "failed": [], "missing": ["intraday_atr"],
            "reference_extreme": None, "reference_level": None,
        }

    lookback = bars[-int(rules["extension_lookback_bars"]) - 1:-1]
    closes = [b["close"] for b in bars]
    rsi = wilder_rsi(closes, int(rules["rsi_period"]))
    prior_rsi, current_rsi = rsi[-2], rsi[-1]
    passed, failed = [], []

    if side == "CALL":
        extreme = max(b["high"] for b in lookback)
        extension_ok = extreme - current["close"] >= float(rules["extension_atr_multiple"]) * atr
        directional_current = current["close"] < current["open"]
        directional_previous = previous["close"] < previous["open"]
        rsi_turn = prior_rsi is not None and current_rsi is not None and prior_rsi < float(rules["rsi_threshold"]) and current_rsi > prior_rsi
        range_contract = directional_current and directional_previous and (current["high"] - current["low"] < previous["high"] - previous["low"])
        higher_structure = previous["low"] > before["low"] and current["close"] > previous["high"]
    else:
        extreme = min(b["low"] for b in lookback)
        extension_ok = current["close"] - extreme >= float(rules["extension_atr_multiple"]) * atr
        directional_current = current["close"] > current["open"]
        directional_previous = previous["close"] > previous["open"]
        rsi_turn = prior_rsi is not None and current_rsi is not None and prior_rsi > float(rules["rsi_threshold"]) and current_rsi < prior_rsi
        range_contract = directional_current and directional_previous and (current["high"] - current["low"] < previous["high"] - previous["low"])
        higher_structure = previous["high"] < before["high"] and current["close"] < previous["low"]

    (passed if extension_ok else failed).append("atr_extension")

    levels = _support_candidates(side, context, current["close"], float(rules["round_number_increment"]))
    proximity = [
        (name, level, abs(current["close"] - level) / level * 100)
        for name, level in levels
    ]
    nearby = [item for item in proximity if item[2] <= float(rules["level_proximity_pct"])]
    (passed if nearby else failed).append("level_proximity")

    momentum_ok = range_contract or rsi_turn
    (passed if momentum_ok else failed).append("momentum_slowing")

    vwap = context.get("session_vwap")
    reclaims = []
    reclaim_levels = levels + ([('session_vwap', float(vwap))] if vwap is not None else [])
    for name, level in reclaim_levels:
        crossed = (previous["close"] <= level < current["close"] if side == "CALL"
                   else previous["close"] >= level > current["close"])
        if crossed:
            reclaims.append((name, level))
    confirmation_ok = higher_structure or bool(reclaims)
    (passed if confirmation_ok else failed).append("causal_confirmation")

    reference_level = min(nearby, key=lambda item: item[2]) if nearby else None
    evidence = {
        "atr": atr, "atr_audit": atr_audit,
        "extension_reference_extreme": extreme,
        "extension_atr_multiple_observed": abs(extreme - current["close"]) / atr,
        "nearby_levels": nearby,
        "range_contraction": range_contract,
        "prior_rsi": prior_rsi, "current_rsi": current_rsi, "rsi_turn": rsi_turn,
        "higher_structure_confirmation": higher_structure,
        "reclaimed_levels": reclaims,
        "decision_bar_bucket": current["bucket"],
        "decision_bar_close": current["close"],
    }
    return {
        "symbol": symbol.upper(), "side": side, "status": "FRESH",
        "passed": passed, "failed": failed, "missing": [],
        "qualified": not failed,
        "direction_ordinal": len(passed) / 4.0,
        "reference_extreme": extreme,
        "reference_extreme_bucket": next(
            b["bucket"] for b in lookback
            if (b["high"] if side == "CALL" else b["low"]) == extreme
        ),
        "reference_level": ({"name": reference_level[0], "price": reference_level[1]}
                            if reference_level else None),
        "evidence": evidence,
    }


def select_contract(*, side: Literal["CALL", "PUT"], contracts: list[dict], decided_at: datetime, rules: dict) -> dict:
    """Deterministic contract selection after explicit execution gates."""
    now = _utc(decided_at)
    accepted, rejected = [], []
    assembly_hashes = sorted({
        str(raw.get("contract_assembly_hash")) for raw in contracts or []
        if raw.get("contract_assembly_hash")
    })
    selection_hash = fe.canonical_hash({
        "contract_selection_version": CONTRACT_SELECTION_VERSION,
        "execution_rules": rules,
        "contract_assembly_hashes": assembly_hashes,
    })
    for raw in contracts or []:
        reasons = []
        provenance = raw.get("field_provenance") or {}

        def field_state(name: str) -> Optional[str]:
            state = (provenance.get(name) or {}).get("state")
            return str(state).upper() if state else None

        right = str(raw.get("option_type") or raw.get("type") or "").upper()
        right = "CALL" if right in {"C", "CALL"} else "PUT" if right in {"P", "PUT"} else right
        if right != side:
            continue
        try:
            quote_at = _utc(raw.get("quote_observed_at") or raw.get("quote_as_of"))
        except Exception:
            quote_at = None
            reasons.append("quote_timestamp_missing")
        bid = float(raw.get("bid") or 0)
        ask = float(raw.get("ask") or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            reasons.append("two_sided_quote_invalid")
        age = (now - quote_at).total_seconds() if quote_at else None
        if age is None or age < 0 or age > float(rules["max_quote_age_seconds"]):
            reasons.append("quote_stale")
        mid = (bid + ask) / 2 if bid > 0 and ask >= bid else None
        spread = (ask - bid) / mid * 100 if mid else None
        if spread is None or spread > float(rules["max_spread_pct"]):
            reasons.append("spread")
        delta = raw.get("delta")
        if delta is None:
            reasons.append("delta_unavailable")
        elif field_state("delta") not in {None, "FRESH"}:
            reasons.append(f"delta_{field_state('delta').lower()}")
        elif not (float(rules["delta_min"]) <= abs(float(delta)) <= float(rules["delta_max"])):
            reasons.append("delta")
        volume = raw.get("volume")
        if volume is None:
            reasons.append("volume_unavailable")
        elif field_state("volume") not in {None, "FRESH"}:
            reasons.append(f"volume_{field_state('volume').lower()}")
        elif int(volume) < int(rules["min_contract_volume"]):
            reasons.append("volume")
        open_interest = raw.get("open_interest", raw.get("oi"))
        if open_interest is None:
            reasons.append("open_interest_unavailable")
        elif field_state("open_interest") not in {None, "FRESH"}:
            reasons.append(f"open_interest_{field_state('open_interest').lower()}")
        elif int(open_interest) < int(rules["min_open_interest"]):
            reasons.append("open_interest")
        if ask <= 0 or ask > float(rules["max_ask_price"]):
            reasons.append("ask_price")
        dte = raw.get("dte")
        if dte is None or not (int(rules["dte_min"]) <= int(dte) <= int(rules["dte_max"])):
            reasons.append("dte")
        if reasons:
            rejected.append({
                "option_symbol": raw.get("option_symbol") or raw.get("symbol"),
                "reasons": sorted(set(reasons)),
                "observed": {
                    key: raw.get(key) for key in (
                        "option_type", "expiry", "dte", "strike", "delta",
                        "delta_source", "bid", "ask", "volume", "open_interest",
                        "quote_observed_at", "quote_received_at",
                        "contract_assembly_version", "contract_assembly_hash")
                },
                "field_provenance": provenance,
            })
            continue
        accepted.append({
            **raw, "option_type": right, "bid": bid, "ask": ask,
            "spread_pct": spread, "quote_observed_at": quote_at,
            "quote_received_at": now,
            "contract_selection_version": CONTRACT_SELECTION_VERSION,
            "contract_selection_hash": selection_hash,
        })
    accepted.sort(key=lambda row: (
        abs(abs(float(row["delta"])) - float(rules["delta_target"])),
        float(row["spread_pct"]), -int(row.get("open_interest") or row.get("oi") or 0),
        str(row.get("option_symbol") or row.get("symbol")),
    ))
    return {
        "selected": accepted[0] if accepted else None,
        "rejected": rejected,
        "eligible_count": len(accepted),
        "source_status": "FRESH",
        "contract_selection_version": CONTRACT_SELECTION_VERSION,
        "contract_selection_hash": selection_hash,
        "contract_assembly_hashes": assembly_hashes,
    }


def option_bid_levels(entry_ask: float, *, risk_fraction: float, reward_risk: float) -> tuple[float, float]:
    """Precompute native option-bid stop/target from the simulated ask fill."""
    if entry_ask <= 0 or not (0 < risk_fraction < 1) or reward_risk <= 0:
        raise ValueError("invalid native option risk geometry")
    risk = entry_ask * risk_fraction
    stop = entry_ask - risk
    target = entry_ask + reward_risk * risk
    return round(stop, 4), round(target, 4)


def episode_key(*, cohort_id: str, symbol: str, side: str, session_date: str,
                reference_extreme_bucket: int, reference_level: Optional[dict]) -> str:
    return fe.canonical_hash({
        "episode_version": EPISODE_VERSION,
        "cohort_id": cohort_id,
        "symbol": symbol.upper(), "side": side, "session_date": session_date,
        "reference_extreme_bucket": int(reference_extreme_bucket),
        "reference_level": reference_level,
    })


def rules_hash(rules: dict) -> str:
    return fe.canonical_hash({"rules_version": RULES_VERSION, "rules": rules})


def build_decision_packet(
    *, cohort_document: dict, setup: dict, contract_selection: dict,
    observed_at: Any, decided_at: Any, config_version: str, config_hash: str,
    raw_evidence_refs: Optional[list[str]] = None,
) -> DecisionPacket:
    """Assemble one causal shadow packet from already-frozen evidence.

    This function does not admit the packet into a cohort; episode admission and
    a valid pre-open lock are separate, fail-closed steps.
    """
    proposed = cohort_document["proposed"]
    side = proposed["side"]
    rules = proposed["direction_rules"]
    execution = proposed["execution"]
    outcome = proposed["outcome"]
    decided = _utc(decided_at)
    observed = _utc(observed_at)

    if setup.get("status") != "FRESH":
        setup_status = setup.get("status")
        if setup_status not in {"STALE", "MISSING", "UNAVAILABLE", "UNUSABLE"}:
            setup_status = "UNUSABLE"
        direction = EvidenceAxis(
            value=None, status=setup_status,
            unavailable=list(setup.get("missing") or ["direction_inputs"]),
        )
        quality = EvidenceAxis(value=None, status=setup_status, unavailable=["direction_setup"])
    else:
        direction = EvidenceAxis(
            value=float(setup.get("direction_ordinal", 0.0)), status="FRESH",
            passed=list(setup.get("passed") or []), failed=list(setup.get("failed") or []),
        )
        evidence = setup.get("evidence") or {}
        quality_checks = {
            "multiple_nearby_levels": len(evidence.get("nearby_levels") or []) >= 2,
            "both_momentum_confirmations": bool(evidence.get("range_contraction") and evidence.get("rsi_turn")),
            "both_structure_confirmations": bool(evidence.get("higher_structure_confirmation")
                                                   and evidence.get("reclaimed_levels")),
        }
        quality = EvidenceAxis(
            value=sum(quality_checks.values()) / len(quality_checks), status="FRESH",
            passed=[name for name, value in quality_checks.items() if value],
            failed=[name for name, value in quality_checks.items() if not value],
        )

    selected = contract_selection.get("selected")
    if selected:
        executability = EvidenceAxis(
            value=1.0, status="FRESH",
            passed=["fresh_two_sided_quote", "spread", "volume", "open_interest",
                    "delta", "ask_price", "dte"],
        )
    elif contract_selection.get("source_status") in {
            "STALE", "MISSING", "UNAVAILABLE", "UNUSABLE"}:
        status = contract_selection["source_status"]
        executability = EvidenceAxis(
            value=None, status=status,
            unavailable=[contract_selection.get("reason") or "option_universe"],
        )
    else:
        failed = sorted({reason for row in contract_selection.get("rejected", [])
                         for reason in row.get("reasons", [])}) or ["no_eligible_contract"]
        executability = EvidenceAxis(value=0.0, status="FRESH", failed=failed)

    scores = EvidenceScoreBlock(direction=direction, quality=quality, executability=executability)
    missing = sorted(set(direction.unavailable + executability.unavailable))
    decision = side if setup.get("qualified") and selected else "NO_TRADE"
    contract_ref = target_ref = stop_ref = None
    ep_key = None
    has_reversal_reference = (
        setup.get("status") == "FRESH"
        and setup.get("reference_extreme_bucket") is not None
    )
    if decision != "NO_TRADE":
        stop, target = option_bid_levels(
            float(selected["ask"]), risk_fraction=float(outcome["option_risk_fraction"]),
            reward_risk=float(outcome["reward_risk"]),
        )
        contract_ref = ContractRef(
            option_symbol=str(selected.get("option_symbol") or selected.get("symbol")),
            option_type=side, expiry=str(selected["expiry"]), dte=int(selected["dte"]),
            strike=float(selected["strike"]), delta=float(selected["delta"]),
            bid=float(selected["bid"]), ask=float(selected["ask"]),
            spread_pct=float(selected["spread_pct"]), volume=int(selected.get("volume") or 0),
            open_interest=int(selected.get("open_interest") or selected.get("oi") or 0),
            quote_observed_at=_utc(selected["quote_observed_at"]),
            quote_received_at=_utc(selected.get("quote_received_at") or decided),
            quote_source=execution["quote_source"],
            contract_selection_version=selected.get(
                "contract_selection_version", CONTRACT_SELECTION_VERSION),
            contract_selection_hash=selected.get("contract_selection_hash"),
            contract_assembly_version=selected.get("contract_assembly_version"),
            contract_assembly_hash=selected.get("contract_assembly_hash"),
            delta_source=selected.get("delta_source"),
            field_provenance=dict(selected.get("field_provenance") or {}),
        )
        target_ref = LevelRef(price=target, rule=outcome["target_bid_formula"])
        stop_ref = LevelRef(price=stop, rule=outcome["stop_bid_formula"])
    else:
        missing.extend(item for item in direction.failed + executability.failed if item not in missing)
    if has_reversal_reference:
        # Trade and rejection candidates share the same reference-derived key,
        # so one reversal cannot populate both datasets.
        ep_key = episode_key(
            cohort_id=proposed["cohort_id"], symbol=setup["symbol"], side=side,
            session_date=decided.date().isoformat(),
            reference_extreme_bucket=int(setup["reference_extreme_bucket"]),
            reference_level=setup.get("reference_level"),
        )
    else:
        ep_key = fe.canonical_hash({
            "episode_version": EPISODE_VERSION,
            "role": "NO_TRADE_OBSERVATION",
            "cohort_id": proposed["cohort_id"],
            "symbol": setup.get("symbol", "").upper(),
            "side": side,
            "decided_at": decided.isoformat(timespec="milliseconds"),
        })

    identity = {
        "schema_version": DECISION_SCHEMA_VERSION, "cohort_id": proposed["cohort_id"],
        "symbol": setup.get("symbol"), "side": side,
        "decided_at": decided.isoformat(timespec="milliseconds"),
        "episode_key": ep_key,
        "rules_hash": rules_hash(rules), "config_hash": config_hash,
    }
    return DecisionPacket(
        decision_id=fe.canonical_hash(identity), cohort_id=proposed["cohort_id"],
        cohort_hash=(cohort_document.get("lock_hash") or cohort_document.get("draft_hash")),
        cohort_lock_status="DRAFT_NOT_LOCKED",
        rules_hash=rules_hash(rules), label_version=outcome["label_version"],
        episode_version=EPISODE_VERSION,
        config_version=config_version, config_hash=config_hash,
        observed_at=observed, decided_at=decided, symbol=setup.get("symbol", "").upper(),
        setup_type=proposed["setup_type"], decision=decision, scores=scores,
        selected_contract=contract_ref, frozen_target=target_ref,
        frozen_invalidation=stop_ref, supporting_evidence=[setup.get("evidence") or {}],
        opposing_evidence=[{"failed": setup.get("failed") or []}],
        missing_or_stale=missing, raw_evidence_refs=raw_evidence_refs or [], episode_key=ep_key,
    )
