"""Request shaping for the Anthropic client (ARCHITECTURE.md §2.7).

The cache-breakpoint assertions here exist because the first live run cached
only 11% of its input tokens: the system block was cached and the conversation
— which is where nearly all the tokens are — was not. That is invisible without
inspecting the request, and expensive at eval-sweep scale.
"""

from __future__ import annotations

from typing import Any

from kubemend.llm.anthropic_client import _split
from kubemend.llm.client import Message


def _conversation() -> list[Message]:
    return [
        Message("system", "SYSTEM PROMPT", pinned=True),
        Message("system", "TASK + SCOPE", pinned=True),
        Message("assistant", "tool_call query_metrics({})"),
        Message("user", "tool_result query_metrics: {...}"),
        Message("assistant", "tool_call search_logs({})"),
        Message("user", "tool_result search_logs: {...}"),
    ]


def _breakpoints(blocks: list[dict[str, Any]]) -> int:
    return sum(1 for b in blocks if "cache_control" in b)


def test_pinned_messages_become_the_system_prefix() -> None:
    system, turns = _split(_conversation())

    assert [b["text"] for b in system] == ["SYSTEM PROMPT", "TASK + SCOPE"]
    assert all(m["role"] in {"user", "assistant"} for m in turns)


def test_two_cache_breakpoints_system_and_conversation_tail() -> None:
    """The conversation breakpoint is the one that pays for itself."""
    system, turns = _split(_conversation())

    assert _breakpoints(system) == 1
    assert "cache_control" in system[-1], "breakpoint sits on the last pinned block"

    tail_blocks = turns[-1]["content"]
    assert "cache_control" in tail_blocks[-1], (
        "without this the growing message list is re-read at full price every turn"
    )


def test_conversation_prefix_is_append_only_and_therefore_cacheable() -> None:
    """Adding a turn must not disturb any earlier byte."""
    before_system, before_turns = _split(_conversation())
    grown = [*_conversation(), Message("assistant", "tool_call get_k8s_state({})")]
    after_system, after_turns = _split(grown)

    def text_of(turns: list[dict[str, Any]]) -> list[str]:
        return [block["text"] for turn in turns for block in turn["content"]]

    assert [b["text"] for b in before_system] == [b["text"] for b in after_system]
    assert text_of(after_turns)[: len(text_of(before_turns))] == text_of(before_turns)


def test_only_the_newest_turn_carries_the_conversation_breakpoint() -> None:
    """More than 4 breakpoints is rejected by the API, so they must not accumulate."""
    _, turns = _split(_conversation())

    marked = sum(1 for turn in turns for block in turn["content"] if "cache_control" in block)
    assert marked == 1


def test_non_pinned_system_messages_become_marked_user_turns() -> None:
    """Sonnet does not support mid-conversation system messages."""
    _, turns = _split(
        [
            Message("system", "PINNED", pinned=True),
            Message("user", "hello"),
            Message("system", "You already have this result."),
        ]
    )

    assert turns[-1]["role"] == "user"
    assert "<system-reminder>" in turns[-1]["content"][-1]["text"]


def test_conversation_always_opens_with_a_user_turn() -> None:
    _, turns = _split([Message("system", "PINNED", pinned=True), Message("assistant", "thinking")])

    assert turns[0]["role"] == "user"
