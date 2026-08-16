"""Anthropic implementation of LLMClient (ARCHITECTURE.md §2.7).

Responsible for prompt-cache breakpoints (after the pinned system+task block and
after the stable conversation prefix) and for per-call usage accounting —
input, cached-input, and output tokens converted to USD via config/pricing.yaml.

The two model tiers come from config: `model.main` runs agent turns,
`model.cheap` runs compaction, handoff, and dev sweeps.
"""

from __future__ import annotations

from typing import Any

import anthropic

from kubemend.config import RunConfig
from kubemend.core.model import ModelTier, ToolCall
from kubemend.llm.client import (
    SYSTEM_REMINDER,
    LLMAuthError,
    LLMError,
    LLMResponse,
    Message,
    Usage,
)

MAX_TOKENS = 16_000


class AnthropicClient:
    """Also serves Bedrock: `AnthropicBedrock` isn't a subclass of
    `Anthropic` (different transport/auth, same `messages.create` surface),
    so the factory constructs it and injects it here via `client=` rather
    than this class knowing anything about Bedrock at all."""

    def __init__(
        self,
        cfg: RunConfig,
        *,
        client: anthropic.Anthropic | anthropic.AnthropicBedrock | None = None,
    ) -> None:
        self._cfg = cfg
        try:
            self._client = client or anthropic.Anthropic()
        except anthropic.AnthropicError as exc:
            raise LLMAuthError(str(exc)) from exc

    def _model_for(self, tier: ModelTier) -> str:
        return self._cfg.model.cheap.name if tier == "cheap" else self._cfg.model.main.name

    def call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        tier: ModelTier = "main",
    ) -> LLMResponse:
        system_blocks, turns = _split(messages)
        request: dict[str, Any] = {
            "model": self._model_for(tier),
            "max_tokens": MAX_TOKENS,
            "system": system_blocks,
            "messages": turns,
        }
        if tools:
            request["tools"] = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["input_schema"],
                }
                for t in tools
            ]

        try:
            response = self._client.messages.create(**request)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMAuthError(str(exc)) from exc
        except anthropic.AnthropicError as exc:
            raise LLMError(str(exc)) from exc

        calls = [
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in response.content
            if block.type == "tool_use"
        ]
        text = "".join(block.text for block in response.content if block.type == "text")
        return LLMResponse(
            text=text,
            tool_calls=calls,
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                cached_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0)
                or 0,
                output_tokens=response.usage.output_tokens,
            ),
            model=response.model,
        )


CACHE_CONTROL = {"type": "ephemeral"}


def _split(messages: list[Message]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate the pinned system prefix from the conversation turns.

    Two breakpoints, per §2.7, and both are needed:

    * one on the last pinned system block, covering the byte-stable
      system + task prefix;
    * one on the final conversation turn, so the *next* request reads the whole
      accumulated investigation from cache.

    The second is the one that matters on this workload. Caching only the system
    block leaves the growing message list to be re-read at full price every
    turn, which on the first live run meant 28k cached tokens against 256k input
    — roughly 11%, where an appended-only conversation should approach 90%.
    """
    system_blocks: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []

    for message in messages:
        if message.pinned:
            system_blocks.append({"type": "text", "text": message.content})
            continue
        role = "assistant" if message.role == "assistant" else "user"
        content = (
            SYSTEM_REMINDER.format(text=message.content)
            if message.role == "system"
            else message.content
        )
        turns.append({"role": role, "content": [{"type": "text", "text": content}]})

    if system_blocks:
        system_blocks[-1]["cache_control"] = dict(CACHE_CONTROL)

    # The API requires the conversation to open with a user turn.
    if not turns or turns[0]["role"] != "user":
        turns.insert(
            0, {"role": "user", "content": [{"type": "text", "text": "Begin the investigation."}]}
        )

    # Breakpoint on the last block of the most recent turn. Everything before it
    # is append-only and therefore byte-stable, which is what makes it cacheable.
    turns[-1]["content"][-1]["cache_control"] = dict(CACHE_CONTROL)
    return system_blocks, turns
