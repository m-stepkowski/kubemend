"""Provider dispatch (ARCHITECTURE.md §2.7).

`cfg.model.{main,cheap}.provider` selects the client per tier — the only
place in the codebase that branches on provider. Everything else (the loop,
the registry, the eval runner) only ever sees an `LLMClient`.
"""

from __future__ import annotations

from typing import Any

import anthropic

from kubemend.config import ModelSpec, RunConfig
from kubemend.core.model import ModelTier
from kubemend.llm.anthropic_client import AnthropicClient
from kubemend.llm.client import LLMAuthError, LLMClient, LLMError, LLMResponse, Message
from kubemend.llm.openai_client import OpenAICompatibleClient


def make_client(cfg: RunConfig) -> LLMClient:
    """Build the client(s) for both tiers and return a single `LLMClient`.

    One client is shared across tiers when both specs resolve to the same
    provider/base_url — most configurations (both tiers on Anthropic, or
    both on the same OpenAI-compatible endpoint) never construct a second
    SDK client or open a second connection pool.
    """
    main_key = _client_key(cfg.model.main)
    cheap_key = _client_key(cfg.model.cheap)

    if main_key == cheap_key:
        shared = _build_one(cfg, cfg.model.main)
        return shared

    return TierRouter(main=_build_one(cfg, cfg.model.main), cheap=_build_one(cfg, cfg.model.cheap))


def _client_key(spec: ModelSpec) -> tuple[str, str | None, str | None]:
    # base_url distinguishes openai-provider endpoints; aws_region does the
    # same job for bedrock. Neither applies to the other provider, but a
    # 3-tuple is simpler than a per-provider key function.
    return (spec.provider, spec.base_url, spec.aws_region)


def _build_one(cfg: RunConfig, spec: ModelSpec) -> LLMClient:
    if spec.provider == "anthropic":
        return AnthropicClient(cfg)
    if spec.provider == "openai":
        return OpenAICompatibleClient(cfg, base_url=spec.base_url)
    if spec.provider == "bedrock":
        try:
            bedrock = anthropic.AnthropicBedrock(aws_region=spec.aws_region)
        except anthropic.AnthropicError as exc:
            raise LLMAuthError(str(exc)) from exc
        return AnthropicClient(cfg, client=bedrock)
    raise LLMError(f"unknown model provider {spec.provider!r}")  # pragma: no cover - Literal-closed


class TierRouter:
    """Forwards each call to the client built for that tier's provider.

    Only needed when main and cheap resolve to different providers — the
    common single-provider case never constructs this.
    """

    def __init__(self, *, main: LLMClient, cheap: LLMClient) -> None:
        self._main = main
        self._cheap = cheap

    def call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        tier: ModelTier = "main",
    ) -> LLMResponse:
        client = self._cheap if tier == "cheap" else self._main
        return client.call(messages, tools=tools, tier=tier)
