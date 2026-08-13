"""Deterministic, read-only evidence explanation for Scalpr (``explain-v0``).

This module is deliberately isolated from execution.  It accepts already-read
evidence dictionaries, creates a typed brief, and renders code-owned sentences.
The optional provider can only propose a constrained ordering of existing facts;
it cannot create facts, wording, sections, priorities, signals, or decisions.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from explanation_provider import DisabledNarrationPlanProvider, NarrationPlanProvider


EXPLAIN_VERSION = "explain-v0"
BRIEF_SCHEMA_VERSION = "evidence-brief-v0"
PLAN_SCHEMA_VERSION = "narration-plan-v0"
VALIDATOR_VERSION = "explain-validator-v0"
RENDERER_VERSION = "explain-renderer-v0"
PROMPT_HASH = "none-deterministic-only"
AUDIT_SCHEMA_VERSION = "explain-audit-v0"
DEFAULT_AUDIT_PATH = Path("explanation_records_v0.jsonl")
_AUDIT_LOCK = threading.Lock()


class DataState(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"
    UNUSABLE = "UNUSABLE"


class FactType(str, Enum):
    STATE = "state"
    COUNT = "count"
    NUMBER = "number"
    SYMBOL = "symbol"
    TIMESTAMP = "timestamp"
    ENUM_LABEL = "enum_label"


class SectionEnum(str, Enum):
    DATA_QUALITY = "data_quality"
    MECHANICAL = "mechanical"
    MISSING_ADVISORY = "missing_or_advisory"
    SYSTEM_OUTPUT = "system_output"
    READING_NOTE = "reading_note"


class StyleEnum(str, Enum):
    SENTENCE = "sentence"
    BULLET = "bullet"


_FACT_ID = re.compile(r"^[a-z0-9_.-]+$")
_ENUM_CODE = re.compile(r"^[A-Za-z0-9_.:-]+$")


class DisplayNote(BaseModel):
    """Free text may be shown locally but is never part of provider input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    note_id: str
    text: str

    @field_validator("note_id")
    @classmethod
    def valid_note_id(cls, value: str) -> str:
        if not _FACT_ID.fullmatch(value):
            raise ValueError("invalid display-note id")
        return value


class EvidenceFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str
    type: FactType
    value: str | int | float | None
    state: Optional[DataState] = None
    section: SectionEnum
    priority: int = Field(ge=0)
    narratable: bool = True
    mandatory: bool = False
    supports: bool = False
    opposes: bool = False

    @field_validator("fact_id")
    @classmethod
    def valid_fact_id(cls, value: str) -> str:
        if not _FACT_ID.fullmatch(value):
            raise ValueError("invalid fact id")
        return value

    @field_validator("value")
    @classmethod
    def reject_boolean_number(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("booleans must be code-owned enum labels")
        return value

    @model_validator(mode="after")
    def state_contract(self):
        if self.type == FactType.STATE:
            if self.state is None or self.value != self.state.value:
                raise ValueError("state facts must carry the same state and value")
        if self.state is not None and self.state != DataState.FRESH:
            if self.type in {FactType.NUMBER, FactType.COUNT} and self.value is not None:
                raise ValueError("non-fresh numeric facts cannot carry a value")
        return self


class EvidenceBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = BRIEF_SCHEMA_VERSION
    explain_version: str = EXPLAIN_VERSION
    symbol: str
    as_of: datetime
    facts: tuple[EvidenceFact, ...]
    display_notes: tuple[DisplayNote, ...] = ()

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        clean = str(value).strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", clean):
            raise ValueError("invalid symbol")
        return clean

    @model_validator(mode="after")
    def unique_facts(self):
        ids = [fact.fact_id for fact in self.facts]
        if len(ids) != len(set(ids)):
            raise ValueError("fact ids must be unique")
        return self


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    fact_id: str
    style: StyleEnum = StyleEnum.SENTENCE


class PlanSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    section: SectionEnum
    items: tuple[PlanItem, ...]


class NarrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = PLAN_SCHEMA_VERSION
    sections: tuple[PlanSection, ...]


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    valid: bool
    errors: tuple[str, ...] = ()


class RequestEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    input_brief_hash: str
    cache_key: str
    provider: str
    model_snapshot_id: str


class ExplanationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    explain_version: str = EXPLAIN_VERSION
    input_brief_hash: str
    cache_key: str
    provider_state: str
    provider: str
    model_snapshot_id: str
    renderer_version: str = RENDERER_VERSION
    template_registry_hash: str
    validator: ValidationResult
    plan: NarrationPlan
    rendered_sections: dict[str, tuple[str, ...]]
    fallback_to_raw_brief: bool
    stale_output_discarded: bool
    outbound_model_calls: int


TEMPLATE_REGISTRY: dict[str, str] = {
    "data_quality.state": "Overall deterministic data state: {value}.",
    "data_quality.forces_abstain": "Formal trade claims must abstain: {value}.",
    "data_quality.nonfresh_count": "Non-fresh required or contextual inputs: {value}.",
    "ei.packet.state": "Latest Entry Intelligence packet state: {value}.",
    "ei.direction.status": "Direction evidence state: {value}.",
    "ei.direction.value": "Direction rule score: {value}; it is not a calibrated probability.",
    "ei.quality.status": "Trade-quality evidence state: {value}.",
    "ei.quality.value": "Trade-quality rule score: {value}; it is not a calibrated probability.",
    "ei.executability.status": "Executability evidence state: {value}.",
    "ei.executability.value": "Executability rule score: {value}; it is not a calibrated probability.",
    "evidence.supporting": "Supporting mechanical evidence codes: {value}.",
    "evidence.opposing": "Opposing mechanical evidence codes: {value}.",
    "premarket.state": "Premarket evidence state: {value}.",
    "premarket.background": "Premarket mechanical background: {value}.",
    "premarket.data_confidence": "Premarket data-coverage score: {value}; it is not a trade probability.",
    "microread.state": "Two-minute tape description state: {value}.",
    "microread.lean": "Two-minute tape description: {value}.",
    "regime.state": "Regime instrumentation state: {value}.",
    "regime.label": "Current descriptive regime label: {value}.",
    "regime.is_descriptive_only": "Regime output is descriptive only: {value}.",
    "flow.uw.state": "Unusual Whales flow state: {value}.",
    "flow.is_qualifying": "Institutional flow is qualifying evidence: {value}.",
    "ivol.state": "IVolatility options-intelligence state: {value}.",
    "collector.state": "Executable-bid collector state: {value}.",
    "entry.decision": "Entry Intelligence decision: {value}.",
    "entry.reason": "Mechanical decision reason code: {value}.",
    "entry.formal_cohort_eligible": "This packet is eligible for a formal cohort claim: {value}.",
    "reading.note": "Descriptive only. Not a probability, signal, or recommendation. Missing inputs are unknown, not neutral.",
}


def canonical_hash(obj: Any) -> str:
    body = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def template_registry_hash(registry: Optional[dict[str, str]] = None) -> str:
    return canonical_hash(registry or TEMPLATE_REGISTRY)


TEMPLATE_REGISTRY_HASH = template_registry_hash()
SCHEMA_HASH = canonical_hash({
    "brief": BRIEF_SCHEMA_VERSION,
    "plan": PLAN_SCHEMA_VERSION,
    "fact_types": [item.value for item in FactType],
    "sections": [item.value for item in SectionEnum],
    "styles": [item.value for item in StyleEnum],
})
VALIDATOR_HASH = canonical_hash({
    "version": VALIDATOR_VERSION,
    "required_ids": [
        "regime.is_descriptive_only", "flow.is_qualifying",
        "data_quality.forces_abstain", "entry.decision", "entry.reason",
    ],
    "coverage": "all_nonfresh_plus_supporting_and_opposing",
    "ordering": "fixed_section_and_nondecreasing_priority",
})


def _utc(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _safe_code(value: Any, fallback: str = "unknown") -> str:
    text = str(value or "").strip()
    if _ENUM_CODE.fullmatch(text):
        return text
    return fallback


def _state(value: Any, default: DataState = DataState.UNAVAILABLE) -> DataState:
    raw = str(value or "").upper()
    aliases = {"AVAILABLE": "FRESH", "READY": "FRESH", "OK": "FRESH"}
    raw = aliases.get(raw, raw)
    try:
        return DataState(raw)
    except ValueError:
        return default


def _age_state(value: Any, *, now: datetime, fresh_seconds: float) -> DataState:
    observed = _utc(value)
    if observed is None:
        return DataState.MISSING
    age = (now - observed).total_seconds()
    if age < -5:
        return DataState.UNUSABLE
    return DataState.FRESH if age <= fresh_seconds else DataState.STALE


def _codes(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    clean = {_safe_code(value, "") for value in values}
    return sorted(value for value in clean if value)


def _fact(
    fact_id: str,
    fact_type: FactType,
    value: str | int | float | None,
    *,
    section: SectionEnum,
    priority: int,
    state: Optional[DataState] = None,
    mandatory: bool = False,
    supports: bool = False,
    opposes: bool = False,
) -> EvidenceFact:
    return EvidenceFact(
        fact_id=fact_id, type=fact_type, value=value, state=state,
        section=section, priority=priority, mandatory=mandatory,
        supports=supports, opposes=opposes,
    )


def _state_fact(fact_id: str, value: DataState, *, section: SectionEnum,
                priority: int, mandatory: bool = False) -> EvidenceFact:
    return _fact(fact_id, FactType.STATE, value.value, state=value,
                 section=section, priority=priority, mandatory=mandatory)


def _component_state(payload: Any, *, now: datetime, timestamp_keys: tuple[str, ...] = (),
                     fresh_seconds: float = 180.0) -> DataState:
    if not isinstance(payload, dict) or not payload:
        return DataState.UNAVAILABLE
    if payload.get("available") is False:
        return DataState.UNAVAILABLE if payload.get("error") else DataState.MISSING
    raw = payload.get("state") or payload.get("status")
    if raw:
        mapped = _state(raw, DataState.FRESH)
        if mapped != DataState.FRESH:
            return mapped
    for key in timestamp_keys:
        if payload.get(key):
            return _age_state(payload[key], now=now, fresh_seconds=fresh_seconds)
    return DataState.FRESH


def _flow_state(payload: Any, *, now: datetime) -> DataState:
    if not isinstance(payload, dict):
        return DataState.UNAVAILABLE
    provider = payload.get("provider") if isinstance(payload.get("provider"), dict) else {}
    if provider.get("configured") is False:
        return DataState.UNAVAILABLE
    status = str(provider.get("status") or "").upper()
    last = payload.get("last_ingestion") if isinstance(payload.get("last_ingestion"), dict) else {}
    last_status = str(last.get("provider_status") or "").upper()
    if last_status == "AVAILABLE":
        return _age_state(last.get("captured_at"), now=now, fresh_seconds=120.0)
    if status == "AVAILABLE":
        # Provider health without a timestamped ingestion is not current flow.
        return DataState.MISSING
    if status in {"UNAVAILABLE", "RATE_LIMITED", "FORBIDDEN", "ERROR"}:
        return DataState.UNAVAILABLE
    return DataState.MISSING


def _ivol_state(payload: Any, *, now: datetime) -> DataState:
    if not isinstance(payload, dict):
        return DataState.UNAVAILABLE
    provider = payload.get("provider") if isinstance(payload.get("provider"), dict) else {}
    if provider.get("configured") is False:
        return DataState.UNAVAILABLE
    capture = payload.get("last_capture") if isinstance(payload.get("last_capture"), dict) else {}
    status = str(capture.get("source_status") or capture.get("status") or "").lower()
    if status in {"ready", "available", "ok"}:
        # IVolatility is an EOD input, so one completed daily capture remains
        # current through the next session but not indefinitely.
        return _age_state(capture.get("captured_at"), now=now, fresh_seconds=36 * 3600)
    if status in {"error", "unavailable", "forbidden"}:
        return DataState.UNAVAILABLE
    return DataState.MISSING


def build_evidence_brief(
    *,
    symbol: str,
    entry_packet: Optional[dict[str, Any]] = None,
    premarket: Optional[dict[str, Any]] = None,
    microread: Optional[dict[str, Any]] = None,
    regime: Optional[dict[str, Any]] = None,
    flow: Optional[dict[str, Any]] = None,
    ivol: Optional[dict[str, Any]] = None,
    collector: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> EvidenceBrief:
    """Build the complete typed brief without inventing absent measurements."""

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    packet = entry_packet if isinstance(entry_packet, dict) else {}
    packet_state = (_age_state(packet.get("decided_at"), now=current, fresh_seconds=180.0)
                    if packet else DataState.MISSING)
    scores = packet.get("scores") if isinstance(packet.get("scores"), dict) else {}
    facts: list[EvidenceFact] = []

    facts.append(_state_fact("ei.packet.state", packet_state,
                             section=SectionEnum.DATA_QUALITY, priority=10))

    axis_states: dict[str, DataState] = {}
    passed: list[str] = []
    failed: list[str] = []
    unavailable: list[str] = []
    for offset, name in enumerate(("direction", "quality", "executability")):
        axis = scores.get(name) if isinstance(scores.get(name), dict) else {}
        recorded_state = _state(axis.get("status"), DataState.MISSING)
        axis_state = recorded_state if packet_state == DataState.FRESH else packet_state
        axis_states[name] = axis_state
        facts.append(_state_fact(
            f"ei.{name}.status", axis_state,
            section=SectionEnum.MECHANICAL, priority=10 + offset,
        ))
        score = axis.get("value")
        if axis_state == DataState.FRESH and isinstance(score, (int, float)) and not isinstance(score, bool):
            facts.append(_fact(
                f"ei.{name}.value", FactType.NUMBER, round(float(score), 4),
                state=DataState.FRESH, section=SectionEnum.MECHANICAL,
                priority=20 + offset,
            ))
        passed.extend(_codes(axis.get("passed")))
        failed.extend(_codes(axis.get("failed")))
        unavailable.extend(_codes(axis.get("unavailable")))

    supporting_codes = sorted(set(passed))
    opposing_codes = sorted(set(failed + unavailable + _codes(packet.get("missing_or_stale"))))
    supporting_value = ",".join(supporting_codes) if supporting_codes else "none_observed"
    opposing_value = ",".join(opposing_codes) if opposing_codes else "none_observed"
    facts.extend([
        _fact("evidence.supporting", FactType.ENUM_LABEL, supporting_value,
              section=SectionEnum.MECHANICAL, priority=30, supports=True, mandatory=True),
        _fact("evidence.opposing", FactType.ENUM_LABEL, opposing_value,
              section=SectionEnum.MECHANICAL, priority=30, opposes=True, mandatory=True),
    ])

    premarket_state = _component_state(
        premarket, now=current, timestamp_keys=("as_of",), fresh_seconds=120.0)
    if isinstance(premarket, dict) and (
            premarket.get("market_status") == "closed" or
            (premarket.get("data_confidence") or {}).get("band") == "unavailable"):
        premarket_state = DataState.UNAVAILABLE
    facts.append(_state_fact("premarket.state", premarket_state,
                             section=SectionEnum.MISSING_ADVISORY, priority=10))
    if premarket_state == DataState.FRESH and isinstance(premarket, dict):
        background = _safe_code(premarket.get("background"), "unknown")
        confidence = (premarket.get("data_confidence") or {}).get("score")
        facts.append(_fact("premarket.background", FactType.ENUM_LABEL, background,
                           section=SectionEnum.MECHANICAL, priority=40))
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            facts.append(_fact("premarket.data_confidence", FactType.NUMBER,
                               round(float(confidence), 4), state=DataState.FRESH,
                               section=SectionEnum.MECHANICAL, priority=41))

    micro_state = _component_state(microread, now=current)
    facts.append(_state_fact("microread.state", micro_state,
                             section=SectionEnum.MISSING_ADVISORY, priority=11))
    if micro_state == DataState.FRESH and isinstance(microread, dict):
        facts.append(_fact("microread.lean", FactType.ENUM_LABEL,
                           _safe_code(microread.get("lean"), "unknown"),
                           section=SectionEnum.MECHANICAL, priority=42))

    regime_state = _component_state(regime, now=current,
                                    timestamp_keys=("generated_at",), fresh_seconds=120.0)
    if (isinstance(regime, dict) and regime.get("market_window_active") is False
            and regime_state == DataState.FRESH):
        # A model can be recomputed after the close, but its last underlying
        # tick is not a current market observation.
        regime_state = DataState.STALE
    facts.append(_state_fact("regime.state", regime_state,
                             section=SectionEnum.MISSING_ADVISORY, priority=12))
    if regime_state == DataState.FRESH and isinstance(regime, dict):
        facts.append(_fact("regime.label", FactType.ENUM_LABEL,
                           _safe_code(regime.get("state"), "unknown"),
                           section=SectionEnum.MECHANICAL, priority=43))
    facts.append(_fact("regime.is_descriptive_only", FactType.ENUM_LABEL, "TRUE",
                       section=SectionEnum.MISSING_ADVISORY, priority=20, mandatory=True))

    flow_state = _flow_state(flow, now=current)
    facts.append(_state_fact("flow.uw.state", flow_state,
                             section=SectionEnum.MISSING_ADVISORY, priority=13))
    facts.append(_fact("flow.is_qualifying", FactType.ENUM_LABEL, "FALSE",
                       section=SectionEnum.MISSING_ADVISORY, priority=21, mandatory=True))

    ivol_state = _ivol_state(ivol, now=current)
    facts.append(_state_fact("ivol.state", ivol_state,
                             section=SectionEnum.MISSING_ADVISORY, priority=14))

    collector_state = _component_state(
        collector, now=current, timestamp_keys=("updated_at",), fresh_seconds=120.0)
    if isinstance(collector, dict) and collector.get("enabled") is False:
        collector_state = DataState.UNAVAILABLE
    facts.append(_state_fact("collector.state", collector_state,
                             section=SectionEnum.MISSING_ADVISORY, priority=15))

    decision = _safe_code(packet.get("decision"), "NO_TRADE") if packet else "NO_TRADE"
    formal_eligible = packet.get("formal_cohort_eligible") is True
    if decision == "NO_TRADE":
        reason_codes = _codes(packet.get("missing_or_stale")) + opposing_codes
        reason = reason_codes[0] if reason_codes else "no_current_entry_intelligence_packet"
    else:
        reason = "mechanical_reversal_qualified"
    facts.extend([
        _fact("entry.decision", FactType.ENUM_LABEL, decision,
              section=SectionEnum.SYSTEM_OUTPUT, priority=10, mandatory=True),
        _fact("entry.reason", FactType.ENUM_LABEL, _safe_code(reason),
              section=SectionEnum.SYSTEM_OUTPUT, priority=11, mandatory=True),
        _fact("entry.formal_cohort_eligible", FactType.ENUM_LABEL,
              "TRUE" if formal_eligible else "FALSE",
              section=SectionEnum.SYSTEM_OUTPUT, priority=12),
        _fact("reading.note", FactType.ENUM_LABEL, "DESCRIPTIVE_ONLY",
              section=SectionEnum.READING_NOTE, priority=0, mandatory=True),
    ])

    context_states = [packet_state, *axis_states.values(), premarket_state, micro_state,
                      regime_state, flow_state, ivol_state, collector_state]
    precedence = {
        DataState.FRESH: 0, DataState.STALE: 1, DataState.MISSING: 2,
        DataState.UNAVAILABLE: 3, DataState.UNUSABLE: 4,
    }
    overall = max(context_states, key=lambda item: precedence[item])
    nonfresh_count = sum(item != DataState.FRESH for item in context_states)
    forces_abstain = (not formal_eligible or decision == "NO_TRADE" or
                      any(state != DataState.FRESH for state in axis_states.values()))
    facts[0:0] = [
        _state_fact("data_quality.state", overall,
                    section=SectionEnum.DATA_QUALITY, priority=0),
        _fact("data_quality.forces_abstain", FactType.ENUM_LABEL,
              "TRUE" if forces_abstain else "FALSE",
              section=SectionEnum.DATA_QUALITY, priority=1, mandatory=True),
        _fact("data_quality.nonfresh_count", FactType.COUNT, nonfresh_count,
              state=DataState.FRESH, section=SectionEnum.DATA_QUALITY, priority=2),
    ]

    timestamps = [_utc(packet.get("decided_at"))]
    for payload, keys in ((premarket, ("as_of",)), (collector, ("updated_at",)),
                          (regime, ("generated_at",))):
        if isinstance(payload, dict):
            timestamps.extend(_utc(payload.get(key)) for key in keys)
    as_of = max((value for value in timestamps if value is not None), default=current)
    return EvidenceBrief(symbol=symbol, as_of=as_of, facts=tuple(facts))


def narratable_fact_ids(brief: EvidenceBrief) -> tuple[str, ...]:
    return tuple(fact.fact_id for fact in brief.facts if fact.narratable)


def sanitized_model_input(brief: EvidenceBrief) -> dict[str, Any]:
    """Enum/typed-only provider input.  Display notes are intentionally absent."""
    return {
        "schema_version": brief.schema_version,
        "symbol": brief.symbol,
        "as_of": brief.as_of.isoformat(),
        "facts": [
            {
                "fact_id": fact.fact_id,
                "type": fact.type.value,
                "value": fact.value,
                "state": fact.state.value if fact.state else None,
                "section": fact.section.value,
                "priority": fact.priority,
            }
            for fact in brief.facts if fact.narratable
        ],
        "allowed_styles": [item.value for item in StyleEnum],
    }


def safe_raw_brief(brief: EvidenceBrief) -> dict[str, Any]:
    payload = brief.model_dump(mode="json")
    for note in payload.get("display_notes", []):
        note["text"] = html.escape(str(note.get("text") or ""), quote=True)
    return payload


def canonical_plan(brief: EvidenceBrief) -> NarrationPlan:
    sections = []
    for section in SectionEnum:
        facts = sorted(
            (fact for fact in brief.facts
             if fact.narratable and fact.section == section),
            key=lambda fact: (fact.priority, fact.fact_id),
        )
        if facts:
            sections.append(PlanSection(
                section=section,
                items=tuple(PlanItem(fact_id=fact.fact_id) for fact in facts),
            ))
    return NarrationPlan(sections=tuple(sections))


def validate_plan(brief: EvidenceBrief, plan: NarrationPlan) -> ValidationResult:
    by_id = {fact.fact_id: fact for fact in brief.facts if fact.narratable}
    expected = set(by_id)
    seen: list[str] = []
    errors: list[str] = []
    seen_sections: set[SectionEnum] = set()
    for section in plan.sections:
        if section.section in seen_sections:
            errors.append(f"duplicate_section:{section.section.value}")
        seen_sections.add(section.section)
        priorities: list[int] = []
        for item in section.items:
            seen.append(item.fact_id)
            fact = by_id.get(item.fact_id)
            if fact is None:
                errors.append(f"unknown_fact:{item.fact_id}")
                continue
            if fact.section != section.section:
                errors.append(f"resectioned_fact:{item.fact_id}")
            priorities.append(fact.priority)
        if priorities != sorted(priorities):
            errors.append(f"priority_violation:{section.section.value}")
    if len(seen) != len(set(seen)):
        errors.append("duplicate_fact")
    missing = sorted(expected - set(seen))
    added = sorted(set(seen) - expected)
    if missing:
        errors.append("missing_facts:" + ",".join(missing))
    if added:
        errors.append("added_facts:" + ",".join(added))

    required = {
        "regime.is_descriptive_only", "flow.is_qualifying",
        "data_quality.forces_abstain", "entry.decision", "entry.reason",
    }
    required.update(fact.fact_id for fact in brief.facts
                    if fact.state is not None and fact.state != DataState.FRESH)
    required.update(fact.fact_id for fact in brief.facts if fact.mandatory)
    if not any(fact.supports for fact in brief.facts):
        errors.append("supporting_evidence_not_defined")
    if not any(fact.opposes for fact in brief.facts):
        errors.append("opposing_evidence_not_defined")
    uncovered = sorted(required - set(seen))
    if uncovered:
        errors.append("mandatory_coverage:" + ",".join(uncovered))
    return ValidationResult(valid=not errors, errors=tuple(errors))


def _display_value(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return format(value, ".4f").rstrip("0").rstrip(".")
    return str(value)


def render_plan(brief: EvidenceBrief, plan: NarrationPlan,
                registry: Optional[dict[str, str]] = None) -> dict[str, tuple[str, ...]]:
    templates = registry or TEMPLATE_REGISTRY
    by_id = {fact.fact_id: fact for fact in brief.facts}
    rendered: dict[str, tuple[str, ...]] = {}
    for section in plan.sections:
        lines = []
        for item in section.items:
            fact = by_id[item.fact_id]
            template = templates.get(item.fact_id)
            if template is None:
                raise ValueError(f"no template registered for {item.fact_id}")
            lines.append(template.format(
                value=_display_value(fact.value),
                state=fact.state.value if fact.state else "",
                symbol=brief.symbol,
            ))
        rendered[section.section.value] = tuple(lines)
    return rendered


def make_cache_key(*, brief_hash: str, provider: str,
                   model_snapshot_id: str) -> str:
    return canonical_hash({
        "brief_hash": brief_hash,
        "prompt_hash": PROMPT_HASH,
        "schema_hash": SCHEMA_HASH,
        "validator_hash": VALIDATOR_HASH,
        "renderer_version": RENDERER_VERSION,
        "template_registry_hash": TEMPLATE_REGISTRY_HASH,
        "provider": provider,
        "model_snapshot_id": model_snapshot_id,
    })


def _append_audit(path: Path, record: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with _AUDIT_LOCK:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class ExplanationService:
    """Orchestrates the inert provider boundary and deterministic fallback."""

    def __init__(self, *, provider: Optional[NarrationPlanProvider] = None,
                 feature_enabled: bool = False,
                 audit_path: Optional[Path] = DEFAULT_AUDIT_PATH):
        self.provider = provider or DisabledNarrationPlanProvider()
        self.feature_enabled = bool(feature_enabled)
        self.audit_path = Path(audit_path) if audit_path is not None else None

    def explain(self, brief: EvidenceBrief, *, request_id: str,
                current_visible_brief_hash: Optional[str] = None) -> ExplanationResult:
        brief_object = brief.model_dump(mode="json")
        brief_hash = canonical_hash({"evidence_brief": brief_object})
        provider_name = str(getattr(self.provider, "provider_name", "none"))
        model_snapshot_id = str(getattr(self.provider, "model_snapshot_id", "none"))
        cache_key = make_cache_key(
            brief_hash=brief_hash, provider=provider_name,
            model_snapshot_id=model_snapshot_id)
        envelope = RequestEnvelope(
            request_id=request_id, input_brief_hash=brief_hash,
            cache_key=cache_key, provider=provider_name,
            model_snapshot_id=model_snapshot_id,
        )
        fallback = False
        provider_state = "DISABLED_DEFAULT_OFF"
        raw_plan: dict[str, Any] | None = None
        if self.feature_enabled and bool(getattr(self.provider, "available", False)):
            try:
                raw_plan = self.provider.create_plan(sanitized_model_input(brief))
                plan = NarrationPlan.model_validate(raw_plan)
                provider_state = "PLAN_RECEIVED"
            except Exception:
                plan = canonical_plan(brief)
                fallback = True
                provider_state = "PROVIDER_FALLBACK"
        else:
            plan = canonical_plan(brief)
            if self.feature_enabled:
                provider_state = "ADAPTER_AVAILABLE_NOT_CONFIGURED"

        validation = validate_plan(brief, plan)
        if not validation.valid:
            plan = canonical_plan(brief)
            validation = validate_plan(brief, plan)
            fallback = True
            provider_state = "VALIDATION_FALLBACK"

        visible_hash = current_visible_brief_hash or brief_hash
        stale = envelope.input_brief_hash != visible_hash
        if stale:
            fallback = True
            provider_state = "STALE_OUTPUT_DISCARDED"
            rendered: dict[str, tuple[str, ...]] = {}
        else:
            rendered = render_plan(brief, plan)

        outbound_calls = int(getattr(self.provider, "outbound_calls", 0))
        result = ExplanationResult(
            input_brief_hash=brief_hash, cache_key=cache_key,
            provider_state=provider_state, provider=provider_name,
            model_snapshot_id=model_snapshot_id,
            template_registry_hash=TEMPLATE_REGISTRY_HASH,
            validator=validation, plan=plan, rendered_sections=rendered,
            fallback_to_raw_brief=fallback,
            stale_output_discarded=stale,
            outbound_model_calls=outbound_calls,
        )
        if self.audit_path is not None:
            _append_audit(self.audit_path, {
                "schema_version": AUDIT_SCHEMA_VERSION,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "explain_version": EXPLAIN_VERSION,
                "prompt_hash": PROMPT_HASH,
                "schema_version_input": BRIEF_SCHEMA_VERSION,
                "validator_version": VALIDATOR_VERSION,
                "renderer_version": RENDERER_VERSION,
                "template_registry_hash": TEMPLATE_REGISTRY_HASH,
                "model_snapshot_id": model_snapshot_id,
                "provider": provider_name,
                "input_brief_hash": brief_hash,
                "request_id": request_id,
                "cache_key": cache_key,
                "raw_plan": raw_plan,
                "effective_plan": plan.model_dump(mode="json"),
                "validator_result": validation.model_dump(mode="json"),
                "provider_state": provider_state,
                "stale_output_discarded": stale,
            })
        return result
