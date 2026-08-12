"""LLMClient Protocol (ARCHITECTURE.md §2.7).

The narrow surface the loop depends on: send a rendered message list plus tool
schemas, get back text, tool calls, and usage. Everything provider-specific —
caching breakpoints, retry policy, token accounting — lives behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from kubemend.core.model import ModelTier, ToolCall

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class Message:
    """One rendered message. `pinned` marks the cacheable prefix (§2.7).

    The context renderer must keep pinned blocks byte-stable across iterations
    or prompt caching silently stops paying for itself.
    """

    role: Role
    content: str
    pinned: bool = False


@dataclass(frozen=True)
class Usage:
    """Per-call token accounting. Cached input is priced separately (§2.7)."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""


class LLMClient(Protocol):
    """Two tiers behind one call: `main` runs agent turns, `cheap` runs
    compaction and handoff. The tier is a parameter rather than a second client
    so the loop never has to know which model is which.
    """

    def call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        tier: ModelTier = "main",
    ) -> LLMResponse: ...
