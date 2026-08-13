"""Provider boundary for the optional explain-v0 narration planner.

The production implementation intentionally ships with only a disabled adapter.
It owns no network client, credential lookup, market-data read, or execution
authority.  A future provider may implement ``create_plan`` without changing the
deterministic evidence brief, validator, or renderer.
"""

from __future__ import annotations

from typing import Any, Protocol


class NarrationPlanProvider(Protocol):
    """Small interface a future OpenAI or Claude adapter must implement."""

    provider_name: str
    model_snapshot_id: str
    available: bool

    def create_plan(self, sanitized_input: dict[str, Any]) -> dict[str, Any]: ...


class DisabledNarrationPlanProvider:
    """Inert default.  Its call counter makes the zero-call invariant testable."""

    provider_name = "none"
    model_snapshot_id = "none"
    available = False

    def __init__(self) -> None:
        self.outbound_calls = 0

    def create_plan(self, sanitized_input: dict[str, Any]) -> dict[str, Any]:
        self.outbound_calls += 1
        raise RuntimeError("no external narration provider is configured")

