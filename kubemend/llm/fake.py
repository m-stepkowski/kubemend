"""Scripted FakeLLM for unit tests (ARCHITECTURE.md §2.7).

Replays a fixed list of turns — tool calls, text, usage — so every loop
behaviour in M1 (truncation, compaction, loop detection, budget exhaustion,
retry policy, the verification-failure path) is exercised deterministically and
offline.

Scripts are split by tier because the loop uses `cheap` for its own bookkeeping
(compaction, handoff) and `main` for agent turns. Keeping them in separate
queues means a test can script three agent turns without also having to predict
whether compaction will fire.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from kubemend.core.model import ModelTier, ToolCall
from kubemend.llm.client import LLMResponse, Message, Usage

_ids = itertools.count(1)

DEFAULT_HANDOFF = {
    "root_cause_hypotheses": [
        {"statement": "unscripted fake handoff", "confidence": 0.1, "evidence": []}
    ],
    "what_was_ruled_out": [],
    "suggested_next_steps": ["script a cheap-tier turn to assert on handoff content"],
    "blocking_reason": None,
}


@dataclass(frozen=True)
class FakeCall:
    """One recorded invocation, so tests can assert on what the loop sent."""

    tier: ModelTier
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rendered(self) -> str:
        return "\n".join(m.content for m in self.messages)


def tool_turn(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    call_id: str | None = None,
    usage: Usage | None = None,
) -> LLMResponse:
    """A turn where the model asks for one tool call."""
    call = ToolCall(id=call_id or f"call-{next(_ids)}", name=name, arguments=arguments or {})
    return LLMResponse(
        tool_calls=[call],
        usage=usage or Usage(input_tokens=100, output_tokens=20),
        model="fake-main",
    )


def text_turn(
    text: str = "I believe the fix is in place.", *, usage: Usage | None = None
) -> LLMResponse:
    """A turn with no tool calls — the model claiming it is done.

    The loop must treat this as a claim to be checked, never as a result (I1).
    """
    return LLMResponse(
        text=text,
        usage=usage or Usage(input_tokens=100, output_tokens=20),
        model="fake-main",
    )


def handoff_turn(**overrides: object) -> LLMResponse:
    report = {**DEFAULT_HANDOFF, **overrides}
    return LLMResponse(text=json.dumps(report), usage=Usage(input_tokens=50, output_tokens=80))


def summary_turn(text: str = "SUMMARY: queries already run: query_metrics(up).") -> LLMResponse:
    return LLMResponse(text=text, usage=Usage(input_tokens=50, output_tokens=40))


class FakeLLMExhausted(AssertionError):
    """Raised when a test's script runs out — always a bug in the test."""


class FakeLLM:
    """Deterministic scripted client. Satisfies the LLMClient Protocol."""

    def __init__(
        self,
        main: Sequence[LLMResponse] = (),
        cheap: Sequence[LLMResponse] | None = None,
    ) -> None:
        self._main = list(main)
        self._cheap = list(cheap or [])
        self.calls: list[FakeCall] = []

    def call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        tier: ModelTier = "main",
    ) -> LLMResponse:
        self.calls.append(FakeCall(tier=tier, messages=list(messages), tools=list(tools or [])))
        if tier == "cheap":
            # Unscripted cheap turns get a valid default so a test asserting on
            # budgets does not also have to script the handoff call.
            return self._cheap.pop(0) if self._cheap else handoff_turn()
        if not self._main:
            raise FakeLLMExhausted(
                f"FakeLLM ran out of main-tier turns after {len(self.calls)} calls; "
                "the loop asked for more than the test scripted"
            )
        return self._main.pop(0)

    @property
    def main_calls(self) -> list[FakeCall]:
        return [c for c in self.calls if c.tier == "main"]

    @property
    def cheap_calls(self) -> list[FakeCall]:
        return [c for c in self.calls if c.tier == "cheap"]
