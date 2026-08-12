"""Handoff on every non-verified termination, and trace round-tripping (§2.6, §7).

A run that ends without a PR is not a crash — it is a report. And the trace is
the source of truth for that report, so recording then replaying it has to yield
exactly what happened, or every downstream fixture built from a trace is fiction.
"""

from __future__ import annotations

from pathlib import Path

from kubemend.config import RunConfig
from kubemend.core.loop import run
from kubemend.core.model import CheckResult, Task, Verdict
from kubemend.llm.fake import FakeLLM, handoff_turn, text_turn, tool_turn
from kubemend.tools.registry import ToolRegistry
from kubemend.trace.recorder import TraceRecorder, replay

from .conftest import StubGate, counting_tool


def _registry() -> ToolRegistry:
    spec, _ = counting_tool("probe", lambda _a: {"ok": True})
    return ToolRegistry([spec])


def test_handoff_is_produced_on_budget_exhaustion(
    task: Task, cfg: RunConfig, trace: TraceRecorder
) -> None:
    cfg.budgets.max_iterations = 2
    llm = FakeLLM(
        main=[tool_turn("probe", {"i": 1}), tool_turn("probe", {"i": 2})],
        cheap=[
            handoff_turn(
                root_cause_hypotheses=[
                    {
                        "statement": "memory limit too low",
                        "confidence": 0.7,
                        "evidence": ["OOMKilled"],
                    }
                ],
                what_was_ruled_out=["image pull failure"],
                suggested_next_steps=["raise resources.limits.memory"],
                blocking_reason="budget_exhausted",
            )
        ],
    )

    result = run(task, cfg, llm=llm, registry=_registry(), gate=StubGate([]), trace=trace)

    assert result.handoff is not None
    assert result.handoff.root_cause_hypotheses[0].statement == "memory limit too low"
    assert result.handoff.what_was_ruled_out == ["image pull failure"]
    assert result.handoff.blocking_reason == "budget_exhausted"
    assert llm.cheap_calls[-1].tools == [], "the handoff call is made without tools"


def test_handoff_survives_an_unparseable_model_reply(
    task: Task, cfg: RunConfig, trace: TraceRecorder
) -> None:
    """A malformed handoff must degrade to a usable report, not crash the run."""
    cfg.budgets.max_iterations = 1
    llm = FakeLLM(main=[tool_turn("probe", {})], cheap=[text_turn("not json at all")])

    result = run(task, cfg, llm=llm, registry=_registry(), gate=StubGate([]), trace=trace)

    assert result.reason == "budget_exhausted"
    assert result.handoff is not None
    assert "not json at all" in " ".join(result.handoff.suggested_next_steps)


def test_verified_run_writes_no_handoff(task: Task, cfg: RunConfig, trace: TraceRecorder) -> None:
    llm = FakeLLM(main=[text_turn("Done.")])
    gate = StubGate([Verdict(passed=True, checks=[CheckResult("all", True, "ok")])])

    result = run(task, cfg, llm=llm, registry=_registry(), gate=gate, trace=trace)

    assert result.handoff is None
    assert llm.cheap_calls == []


def test_trace_replays_to_an_identical_event_sequence(
    task: Task, cfg: RunConfig, tmp_path: Path
) -> None:
    cfg.budgets.max_iterations = 3
    path = tmp_path / "run.jsonl"
    trace = TraceRecorder.open(path)
    llm = FakeLLM(main=[tool_turn("probe", {"i": 1}), text_turn("Done.")])
    gate = StubGate([Verdict(passed=True, checks=[CheckResult("all", True, "ok")])])

    run(task, cfg, llm=llm, registry=_registry(), gate=gate, trace=trace)

    assert replay(path) == trace.events


def test_trace_records_the_shape_of_the_run(task: Task, cfg: RunConfig, tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    trace = TraceRecorder.open(path)
    llm = FakeLLM(main=[tool_turn("probe", {"i": 1}), text_turn("Done.")])
    gate = StubGate([Verdict(passed=True, checks=[CheckResult("all", True, "ok")])])

    run(task, cfg, llm=llm, registry=_registry(), gate=gate, trace=trace)

    kinds = [event["type"] for event in replay(path)]
    assert kinds[0] == "run_header"
    assert "model_turn" in kinds
    assert "tool_call" in kinds
    assert "verdict" in kinds
    assert kinds[-1] == "result"

    tool_events = [e for e in replay(path) if e["type"] == "tool_call"]
    assert tool_events[0]["raw_bytes"] > 0
    assert "duration_ms" in tool_events[0]
