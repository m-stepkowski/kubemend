"""Loop detector (ARCHITECTURE.md §2.5).

Identical consecutive calls are never productive: the model has the answer and
has forgotten it. Nudge on the second, abort on the third. Crucially the second
call is *not executed* — re-running it would spend budget to produce a payload
already in context.
"""

from __future__ import annotations

from pathlib import Path

from kubemend.config import RunConfig
from kubemend.core.loop import run
from kubemend.core.loop_detector import LoopDetector
from kubemend.core.model import Task, ToolCall
from kubemend.llm.fake import FakeLLM, tool_turn
from kubemend.tools.registry import ToolRegistry
from kubemend.trace.recorder import TraceRecorder

from .conftest import StubGate, counting_tool


def test_nudges_on_second_identical_call_and_aborts_on_third() -> None:
    detector = LoopDetector(warn_after=2, abort_after=3)
    call = ToolCall(id="a", name="query_metrics", arguments={"promql": "up"})

    assert detector.observe(call) is None
    assert detector.should_abort() is False

    nudge = detector.observe(ToolCall(id="b", name="query_metrics", arguments={"promql": "up"}))
    assert nudge is not None
    assert detector.should_abort() is False

    detector.observe(ToolCall(id="c", name="query_metrics", arguments={"promql": "up"}))
    assert detector.should_abort() is True


def test_argument_order_does_not_defeat_the_signature() -> None:
    """Signatures are canonical JSON, so a reordered dict is still a repeat."""
    detector = LoopDetector()
    detector.observe(ToolCall(id="a", name="t", arguments={"x": 1, "y": 2}))

    assert detector.observe(ToolCall(id="b", name="t", arguments={"y": 2, "x": 1})) is not None


def test_a_different_call_resets_the_streak() -> None:
    detector = LoopDetector()
    detector.observe(ToolCall(id="a", name="t", arguments={"x": 1}))
    detector.observe(ToolCall(id="b", name="t", arguments={"x": 1}))
    detector.observe(ToolCall(id="c", name="t", arguments={"x": 2}))

    assert detector.should_abort() is False
    assert detector.observe(ToolCall(id="d", name="t", arguments={"x": 2})) is not None


def test_loop_skips_execution_of_the_repeated_call_and_aborts(
    task: Task, cfg: RunConfig, tmp_path: Path
) -> None:
    spec, attempts = counting_tool("probe", lambda _a: {"value": 1})
    registry = ToolRegistry([spec])
    llm = FakeLLM(
        main=[
            tool_turn("probe", {"q": "same"}),
            tool_turn("probe", {"q": "same"}),
            tool_turn("probe", {"q": "same"}),
        ]
    )
    trace = TraceRecorder.open(tmp_path / "run.jsonl")

    result = run(task, cfg, llm=llm, registry=registry, gate=StubGate([]), trace=trace)

    assert result.reason == "loop_detected"
    assert result.success is False
    assert len(attempts) == 1, "the repeated calls must not be executed again"
    assert result.handoff is not None, "an aborted run still hands off what it learned"
