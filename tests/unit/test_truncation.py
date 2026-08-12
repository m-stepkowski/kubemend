"""Per-result truncation (ARCHITECTURE.md §2.3).

Head 60 / tail 40 rather than head-only, because errors cluster at both ends of
a log or query window — head-only truncation loses the final stack trace, which
is usually the most diagnostic line in the payload.
"""

from __future__ import annotations

import json
from typing import Any

from kubemend.core.context import BYTES_PER_TOKEN
from kubemend.core.model import ToolCall
from kubemend.tools.base import ToolSpec
from kubemend.tools.registry import TRUNCATION_MARKER, ToolRegistry


def _big_tool(payload: dict[str, Any]) -> ToolSpec:
    return ToolSpec(
        name="big",
        description="Returns a large payload.",
        parameters={"type": "object", "properties": {}},
        executor=lambda _args: payload,
    )


def test_truncation_keeps_head_60_tail_40_with_splice_marker() -> None:
    cap_tokens = 100
    cap_bytes = cap_tokens * BYTES_PER_TOKEN
    payload = {"lines": ["head-marker", *["filler" * 20] * 200, "tail-marker"]}
    registry = ToolRegistry([_big_tool(payload)], result_token_cap=cap_tokens)

    outcome = registry.execute(ToolCall(id="c1", name="big", arguments={}))

    serialized = json.dumps(payload, sort_keys=True)
    assert outcome.truncated is True
    assert outcome.raw_bytes == len(serialized)

    content = outcome.payload["content"]
    marker = TRUNCATION_MARKER.format(raw_bytes=len(serialized))
    assert marker in content

    head, tail = content.split(marker)
    assert len(head) == int(cap_bytes * 0.6)
    assert len(tail) == cap_bytes - int(cap_bytes * 0.6)

    # The kept fragments are verbatim slices of the real payload, from both ends.
    assert serialized.startswith(head)
    assert serialized.endswith(tail)
    assert "head-marker" in head
    assert "tail-marker" in tail


def test_small_payload_passes_through_untruncated() -> None:
    payload = {"ok": True}
    registry = ToolRegistry([_big_tool(payload)], result_token_cap=6000)

    outcome = registry.execute(ToolCall(id="c1", name="big", arguments={}))

    assert outcome.truncated is False
    assert outcome.payload == payload
    assert outcome.raw_bytes == len(json.dumps(payload, sort_keys=True))


def test_marker_names_the_raw_size_so_the_model_can_narrow() -> None:
    """The splice text is a teaching signal, not decoration: it must carry the
    real byte count and tell the model what to do about it (§2.3)."""
    payload = {"blob": "x" * 5000}
    registry = ToolRegistry([_big_tool(payload)], result_token_cap=50)

    outcome = registry.execute(ToolCall(id="c1", name="big", arguments={}))

    content = outcome.payload["content"]
    assert str(outcome.raw_bytes) in content
    assert "Narrow the query" in content
