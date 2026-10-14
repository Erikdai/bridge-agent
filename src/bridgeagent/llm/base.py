from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..actions import Action
from ..config import Settings


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(description="One or two sentences: what you see and why this action")
    note: str | None = Field(
        default=None,
        description="A fact to remember for later steps (e.g. values read from a page). Shown back to you on every following step, on every surface.",
    )
    action: Action


class PlannerRefused(RuntimeError):
    pass


class SkillCompaction(BaseModel):
    """Which recorded steps are worth keeping as a reusable skill."""

    model_config = ConfigDict(extra="ignore")

    reasoning: str = Field(default="", description="Why these steps and not the others")
    keep: list[int] = Field(description="1-based indices of the steps to keep, in execution order")


class Planner(Protocol):
    model: str

    def decide(self, system: str, user_text: str, screenshot_png: bytes | None) -> tuple[Decision, dict]:
        """Return the decision plus a usage dict (input_tokens, output_tokens, cache_read_input_tokens)."""
        ...

    def ask(self, system: str, user_text: str, schema: type[BaseModel]) -> tuple[BaseModel, dict]:
        """One structured call with an arbitrary schema (used for skill compaction)."""
        ...


def make_planner(settings: Settings) -> Planner:
    if settings.provider == "anthropic":
        from .anthropic_backend import AnthropicPlanner

        return AnthropicPlanner(model=settings.model, effort=settings.effort)
    if settings.provider == "openai":
        if not settings.model:
            raise RuntimeError("set OPENAI_MODEL (or BRIDGE_MODEL) for the OpenAI-compatible backend")
        from .openai_backend import OpenAIPlanner

        return OpenAIPlanner(model=settings.model)
    raise RuntimeError(f"unknown provider {settings.provider!r}")
