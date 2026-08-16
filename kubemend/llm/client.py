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

# Every provider renders non-pinned system messages (loop nudges,
# verification failures) the same way: wrapped and demoted to a user turn,
# because mid-conversation system messages aren't universally supported (not
# on Sonnet; OpenAI-compatible APIs do accept them, but wrapping anyway keeps
# the rendered conversation shape byte-identical across providers, which is
# what makes eval traces comparable across a provider swap).
SYSTEM_REMINDER = "<system-reminder>{text}</system-reminder>"


class LLMError(Exception):
    """A provider call failed. Providers wrap their SDK's own exception type
    here (`raise LLMError(...) from exc`) so callers — `cli.py`,
    `evals/runner.py`, `core/loop.py` — never need to import a provider SDK
    just to catch its errors."""


class LLMAuthError(LLMError):
    """Credentials missing or rejected, at client construction or at call
    time. Distinct from `LLMError` so a caller can print a
    credential-specific hint (which env var, which SDK) rather than a
    generic failure message."""


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
    """Per-call token accounting. Cached input is priced separately (§2.7).

    Normative contract every client must uphold: `input_tokens` EXCLUDES
    `cached_input_tokens` (Anthropic's native semantics). `trace/cost.py`
    sums all four fields additively, so a provider whose SDK reports a
    prompt-token count that *includes* cached tokens (OpenAI, DeepSeek) must
    subtract the cached count before setting `input_tokens`, or costs are
    double-counted.
    """

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
