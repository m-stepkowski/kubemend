"""OpenAI-compatible implementation of LLMClient (ARCHITECTURE.md §2.7).

Covers OpenAI itself and anything speaking the same `/v1/chat/completions`
dialect — DeepSeek, vLLM, Ollama, or any other OpenAI-compatible endpoint —
selected via `provider: openai` plus an optional `base_url` on `ModelSpec`.
One client instance serves both tiers, the same way `AnthropicClient` does:
`_model_for(tier)` resolves the model name per call from the whole config,
not from a single fixed spec.
"""

from __future__ import annotations

import json
import os
from typing import Any

import openai

from kubemend.config import RunConfig
from kubemend.core.model import ModelTier, ToolCall
from kubemend.llm.client import SYSTEM_REMINDER, LLMAuthError, LLMError, LLMResponse, Message, Usage

MAX_TOKENS = 16_000

# The malformed-arguments marker a caller (registry.validate_arguments) will
# reject on the next turn, sending the parse failure back to the model as a
# normal tool error rather than crashing the run. Local models in particular
# sometimes emit tool-call arguments that aren't valid JSON.
MALFORMED_ARGUMENTS_KEY = "__malformed_arguments__"


class OpenAICompatibleClient:
    def __init__(
        self,
        cfg: RunConfig,
        *,
        base_url: str | None = None,
        client: openai.OpenAI | None = None,
    ) -> None:
        self._cfg = cfg
        self._base_url = base_url
        try:
            self._client = client or openai.OpenAI(
                base_url=base_url, api_key=_resolve_api_key(base_url)
            )
        except openai.OpenAIError as exc:
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
        request: dict[str, Any] = {
            "model": self._model_for(tier),
            "messages": _render_messages(messages),
        }
        # Reasoning-model families (gpt-5-class on the real OpenAI API) reject
        # `max_tokens` in favor of `max_completion_tokens`; custom base_urls
        # (DeepSeek, vLLM, Ollama) are keyed on `base_url is not None` here
        # since a compat server needing the newer param would be a follow-up,
        # not something this heuristic can detect offline.
        if self._base_url is None:
            request["max_completion_tokens"] = MAX_TOKENS
        else:
            request["max_tokens"] = MAX_TOKENS
        if tools:
            request["tools"] = _translate_tools(tools)
            # Known gap, found running this against the real API (M7): some
            # OpenAI reasoning-tier models (gpt-5.6-luna confirmed) reject
            # tool calls on this endpoint unless `reasoning_effort: "none"`
            # is set — but sending that param to a non-reasoning model is
            # itself a 400. There's no reliable way to tell which kind a
            # given model string is without a hardcoded, ever-stale list, so
            # this client does not set it. The API's own error message names
            # the fix; a reasoning-tier model needs `reasoning_effort: none`
            # configured by whoever picks that model, not guessed here.

        try:
            response = self._client.chat.completions.create(**request)
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMAuthError(str(exc)) from exc
        except openai.OpenAIError as exc:
            raise LLMError(str(exc)) from exc

        message = response.choices[0].message
        calls = [
            ToolCall(
                id=tc.id, name=tc.function.name, arguments=_parse_arguments(tc.function.arguments)
            )
            for tc in message.tool_calls or []
        ]
        return LLMResponse(
            text=message.content or "",
            tool_calls=calls,
            usage=_usage_from(response.usage),
            model=response.model,
        )


def _resolve_api_key(base_url: str | None) -> str | None:
    """None lets the SDK read OPENAI_API_KEY itself (and raise its own clear
    error if unset) — matches AnthropicClient's "let the SDK decide" stance.
    Only when there's no key at all AND a custom base_url is set do we
    substitute a placeholder: local/self-hosted endpoints (vLLM, Ollama)
    commonly don't check it, and a real remote endpoint (DeepSeek) that does
    still requires the caller to set OPENAI_API_KEY, same as OpenAI itself."""
    if os.environ.get("OPENAI_API_KEY"):
        return None
    if base_url is not None:
        return "unused"
    return None


def _render_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Mirrors `anthropic_client._split()`'s shape and synthetic-opener rule
    so the rendered conversation is comparable across providers, flattened
    into the single message list this API expects (no separate system
    array — `pinned` just means "leading role=system", caching is
    automatic on this side)."""
    system: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    for message in messages:
        if message.pinned:
            system.append({"role": "system", "content": message.content})
            continue
        role = "assistant" if message.role == "assistant" else "user"
        content = (
            SYSTEM_REMINDER.format(text=message.content)
            if message.role == "system"
            else message.content
        )
        turns.append({"role": role, "content": content})

    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": "Begin the investigation."})
    return system + turns


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def _parse_arguments(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {MALFORMED_ARGUMENTS_KEY: raw}
    if not isinstance(parsed, dict):
        return {MALFORMED_ARGUMENTS_KEY: raw}
    return parsed


def _usage_from(usage: object) -> Usage:
    if usage is None:
        return Usage()
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    if not cached:
        # DeepSeek's own field names, outside the OpenAI response schema —
        # present only if the SDK's model happens to pass extra fields
        # through; documented seam, not a guaranteed read (see ARCHITECTURE.md).
        cached = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    return Usage(
        input_tokens=prompt_tokens - cached,
        cached_input_tokens=cached,
        cache_creation_tokens=0,
        output_tokens=completion_tokens,
    )
