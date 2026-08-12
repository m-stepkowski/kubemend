"""Run budgets (ARCHITECTURE.md §2.2, invariant I4).

Three limits, checked at the top of every turn. Each must be able to terminate a
run on its own and name itself in the result, because "the run stopped" is not
actionable but "the run stopped after $1.00" is.
"""

from __future__ import annotations

from kubemend.config import RunConfig
from kubemend.core.budget import Budget
from kubemend.core.loop import run
from kubemend.core.model import Task
from kubemend.llm.client import LLMResponse, Usage
from kubemend.llm.fake import FakeLLM, tool_turn
from kubemend.tools.registry import ToolRegistry
from kubemend.trace.recorder import TraceRecorder

from .conftest import StubGate, counting_tool


def _registry() -> ToolRegistry:
    spec, _ = counting_tool("probe", lambda _a: {"ok": True})
    return ToolRegistry([spec])


def _turns(n: int, *, usage: Usage | None = None) -> list[LLMResponse]:
    return [tool_turn("probe", {"i": i}, usage=usage) for i in range(n)]


def test_iteration_limit_terminates_with_its_own_reason(
    task: Task, cfg: RunConfig, trace: TraceRecorder
) -> None:
    cfg.budgets.max_iterations = 3
    llm = FakeLLM(main=_turns(10))

    result = run(task, cfg, llm=llm, registry=_registry(), gate=StubGate([]), trace=trace)

    assert result.reason == "budget_exhausted"
    assert result.iterations == 3
    assert len(llm.main_calls) == 3


def test_cost_limit_terminates_with_its_own_reason(
    task: Task, cfg: RunConfig, trace: TraceRecorder
) -> None:
    cfg.budgets.max_iterations = 100
    cfg.model.main.max_cost_usd_per_run = 0.0001
    expensive = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    llm = FakeLLM(main=_turns(10, usage=expensive))

    result = run(task, cfg, llm=llm, registry=_registry(), gate=StubGate([]), trace=trace)

    assert result.reason == "budget_exhausted"
    assert result.cost_usd > 0.0
    assert len(llm.main_calls) < 10, "cost must stop the run before the script runs out"


def test_wall_clock_limit_terminates_with_its_own_reason(
    task: Task, cfg: RunConfig, trace: TraceRecorder
) -> None:
    cfg.budgets.max_wall_seconds = 0
    llm = FakeLLM(main=_turns(10))

    result = run(task, cfg, llm=llm, registry=_registry(), gate=StubGate([]), trace=trace)

    assert result.reason == "budget_exhausted"
    assert len(llm.main_calls) == 0, "an already-spent wall clock stops before the first call"


def test_budget_names_the_limit_that_tripped() -> None:
    iteration_bound = Budget(max_iterations=2, max_cost_usd=1.0, max_wall_seconds=600)
    assert iteration_bound.exhausted() is None
    iteration_bound.tick()
    iteration_bound.tick()
    assert iteration_bound.exhausted() == "max_iterations"

    cost_bound = Budget(max_iterations=100, max_cost_usd=0.5, max_wall_seconds=600)
    cost_bound.charge_usd(0.75)
    assert cost_bound.exhausted() == "max_cost_usd"

    time_bound = Budget(max_iterations=100, max_cost_usd=1.0, max_wall_seconds=0)
    assert time_bound.exhausted() == "max_wall_seconds"
