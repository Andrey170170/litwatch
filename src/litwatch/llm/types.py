"""The neutral request and reply every provider adapter speaks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel

from litwatch.config import RoleConfig


@dataclass(frozen=True)
class Request:
    """What is asked. Effort is not part of it; that comes only from the role's configuration."""

    system: str
    user: str
    output: type[BaseModel]  # the reply must be JSON matching this model
    max_output_tokens: int | None = None

    def key_material(self) -> dict[str, Any]:
        """What identifies the request for reuse. Schema key order is kept: it can change the
        rendered model input."""
        return {
            "system": self.system,
            "user": self.user,
            "schema": self.output.model_json_schema(),
            "max_output_tokens": self.max_output_tokens,
        }


class Stop(StrEnum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    REFUSED = "refused"


class Usage(BaseModel):
    """One model call's accounting. `cost` is in USD; None means unknown, never zero."""

    model: str
    via: str | None = None  # the account the call went through: anthropic | openrouter
    effort: str | None = None
    prompt_tokens: int = 0  # all input tokens, cached ones included
    completion_tokens: int = 0
    cost: float | None = None
    generation_id: str | None = None
    provider: str | None = None  # who served it, as the account reports
    service_tier: str | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    # A reused reply keeps its original accounting; reuse charges this run nothing.
    reused: bool = False
    original_cost: float | None = None
    source_call_id: int | None = None


@dataclass(frozen=True)
class Reply:
    text: str
    stop: Stop
    usage: Usage
    refusal: str | None = None  # the category given, when `stop` is refused


class ProviderError(Exception):
    """A provider did not answer. `transient` failures are worth another attempt."""

    def __init__(self, message: str, *, transient: bool = False, timeout: bool = False) -> None:
        super().__init__(message)
        self.transient = transient
        self.timeout = timeout


class Provider(Protocol):
    """One account that serves models. Adapters translate the request, the reply and the
    failures, and decide nothing else."""

    name: str

    async def complete(self, role: RoleConfig, request: Request) -> Reply: ...

    def routing(self, role: RoleConfig) -> tuple[str, ...]:
        """What else decides who serves the request; part of the reuse key."""
        ...
