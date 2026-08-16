"""Provider dispatch and the fatal_error termination path (ARCHITECTURE.md §2.7).

`make_client`/`TierRouter` are the only place that branches on
`ModelSpec.provider`; a bad or unreachable provider must surface as a plain
`LLMError`/`LLMAuthError`, never a raw SDK exception, so `cli.py` and
`evals/runner.py` can print one consistent hint regardless of which provider
failed.
"""

from __future__ import annotations

from typing import Any

import pytest

from kubemend.config import ModelConfig, ModelSpec, RunConfig
from kubemend.core.loop import run
from kubemend.core.model import Task
from kubemend.llm.client import LLMClient, LLMError, LLMResponse, Message
from kubemend.llm.factory import TierRouter, make_client
from kubemend.tools.registry import ToolRegistry
from kubemend.trace.recorder import TraceRecorder

from .conftest import StubGate


class _RaisingLLM:
    """Fails every call — stands in for an unreachable/misconfigured provider."""

    def __init__(self, exc: LLMError) -> None:
        self._exc = exc
        self.calls = 0

    def call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        tier: str = "main",
    ) -> LLMResponse:
        self.calls += 1
        raise self._exc


class _RecordingLLM:
    """Records which tier it was called for; returns a fixed no-op response."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.tiers_seen: list[str] = []

    def call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        tier: str = "main",
    ) -> LLMResponse:
        self.tiers_seen.append(tier)
        return LLMResponse(text=self.name)


def test_anthropic_provider_dispatches_to_anthropic_client() -> None:
    from kubemend.llm.anthropic_client import AnthropicClient
    from kubemend.llm.factory import _build_one

    cfg = RunConfig(model=ModelConfig(main=ModelSpec(provider="anthropic", name="claude-x")))

    client = _build_one(cfg, cfg.model.main)

    assert isinstance(client, AnthropicClient)


def test_openai_provider_dispatches_to_openai_compatible_client() -> None:
    from kubemend.llm.factory import _build_one
    from kubemend.llm.openai_client import OpenAICompatibleClient

    cfg = RunConfig(
        model=ModelConfig(main=ModelSpec(provider="openai", name="gpt-x", base_url="http://x"))
    )

    client = _build_one(cfg, cfg.model.main)

    assert isinstance(client, OpenAICompatibleClient)


def test_bedrock_provider_dispatches_to_anthropic_client_with_a_bedrock_sdk_client() -> None:
    """AnthropicBedrock isn't a subclass of Anthropic, but AnthropicClient
    serves both — the factory is what tells them apart."""
    from kubemend.llm.anthropic_client import AnthropicClient
    from kubemend.llm.factory import _build_one

    cfg = RunConfig(
        model=ModelConfig(main=ModelSpec(provider="bedrock", name="us.anthropic.claude-x"))
    )

    client = _build_one(cfg, cfg.model.main)

    assert isinstance(client, AnthropicClient)


def test_bedrock_tiers_with_different_aws_regions_are_not_shared() -> None:
    """Two bedrock specs with the same provider but different aws_region must
    build two clients, the same way different base_urls do for openai."""
    from kubemend.llm import factory

    cfg = RunConfig(
        model=ModelConfig(
            main=ModelSpec(provider="bedrock", name="x", aws_region="us-east-1"),
            cheap=ModelSpec(provider="bedrock", name="x", aws_region="eu-west-1"),
        )
    )

    assert factory._client_key(cfg.model.main) != factory._client_key(cfg.model.cheap)


def test_tier_router_forwards_main_and_cheap_to_their_own_client() -> None:
    main_client = _RecordingLLM("main-client")
    cheap_client = _RecordingLLM("cheap-client")
    router: LLMClient = TierRouter(main=main_client, cheap=cheap_client)

    main_result = router.call([Message("user", "hi")], tier="main")
    cheap_result = router.call([Message("user", "hi")], tier="cheap")

    assert main_result.text == "main-client"
    assert cheap_result.text == "cheap-client"
    assert main_client.tiers_seen == ["main"]
    assert cheap_client.tiers_seen == ["cheap"]


def test_make_client_shares_one_client_when_both_tiers_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two tiers on the same provider+base_url must not construct two SDK
    clients — most configs (both tiers on Anthropic) are this case."""
    from kubemend.llm import factory

    built: list[ModelSpec] = []
    original = factory._build_one

    def _tracking_build(cfg: RunConfig, spec: ModelSpec) -> LLMClient:
        built.append(spec)
        raise LLMError("stub: construction not needed for this assertion")

    monkeypatch.setattr(factory, "_build_one", _tracking_build)
    cfg = RunConfig(
        model=ModelConfig(
            main=ModelSpec(provider="anthropic", name="same-model"),
            cheap=ModelSpec(provider="anthropic", name="same-model"),
        )
    )

    with pytest.raises(LLMError):
        make_client(cfg)

    assert len(built) == 1, "matching (provider, base_url) must build exactly one client"
    monkeypatch.setattr(factory, "_build_one", original)


def test_llm_error_mid_run_ends_in_fatal_error_not_a_crash(
    task: Task, cfg: RunConfig, trace: TraceRecorder
) -> None:
    """An unreachable/misconfigured provider must not crash the loop — it is
    a structured, reportable outcome, distinct from a verification failure."""
    llm = _RaisingLLM(LLMError("connection refused"))
    registry = ToolRegistry([])
    gate = StubGate([])

    result = run(task, cfg, llm=llm, registry=registry, gate=gate, trace=trace)

    assert result.success is False
    assert result.reason == "fatal_error"
    assert result.handoff is not None
    assert "connection refused" in (result.handoff.blocking_reason or "")
    assert llm.calls == 1, "no retry inside the loop; retries are the caller's decision"
