"""Shared fixtures for the M1 harness tests.

The toy tools (`echo`, `fail_with`) live here rather than in the package: they
exist to exercise the executor wrapper, and shipping fake tools inside
kubemend/ would put something in the registry that must never reach a real run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kubemend.config import BudgetConfig, ContextConfig, ModelConfig, ModelSpec, RunConfig
from kubemend.core.model import CheckResult, Scope, Task, Verdict
from kubemend.tools.base import ClientError, ToolSpec, TransportError
from kubemend.trace.recorder import TraceRecorder


@pytest.fixture
def task() -> Task:
    return Task(
        statement="shop-api pods are crash-looping",
        scope=Scope(namespace="shop", app="shop-api"),
    )


@pytest.fixture
def cfg() -> RunConfig:
    """Small budgets by default so tests terminate fast and explicitly."""
    return RunConfig(
        model=ModelConfig(
            main=ModelSpec(name="fake-main", max_cost_usd_per_run=1.00),
            cheap=ModelSpec(name="fake-cheap"),
        ),
        budgets=BudgetConfig(max_iterations=15, max_wall_seconds=600),
        context=ContextConfig(
            result_token_cap=6000,
            compact_threshold=0.70,
            model_window_tokens=200_000,
        ),
    )


@pytest.fixture
def trace(tmp_path: Path) -> TraceRecorder:
    return TraceRecorder.open(tmp_path / "run.jsonl")


def echo_tool() -> ToolSpec:
    """Returns its arguments. Useful for payload-shape and truncation tests."""

    def _echo(args: dict[str, Any]) -> dict[str, Any]:
        return dict(args)

    return ToolSpec(
        name="echo",
        description="Echo the arguments back.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        executor=_echo,
    )


def counting_tool(
    name: str,
    behaviour: Callable[[int], dict[str, Any]],
) -> tuple[ToolSpec, list[int]]:
    """A tool that records how many times it was actually invoked.

    `behaviour` receives the 1-based attempt number and either returns a payload
    or raises. The returned list holds one entry per invocation, which is how
    the retry tests distinguish "retried once" from "retried forever".
    """
    attempts: list[int] = []

    def _executor(args: dict[str, Any]) -> dict[str, Any]:
        attempts.append(len(attempts) + 1)
        return behaviour(len(attempts))

    spec = ToolSpec(
        name=name,
        description="Test tool.",
        parameters={"type": "object", "properties": {}},
        executor=_executor,
    )
    return spec, attempts


def fail_with(error_type: str) -> tuple[ToolSpec, list[int]]:
    """Toy tool that always raises the requested error class."""

    def _behaviour(_attempt: int) -> dict[str, Any]:
        if error_type == "transport":
            raise TransportError("upstream reset the connection")
        raise ClientError("the selector is malformed")

    return counting_tool(f"fail_with_{error_type}", _behaviour)


class StubGate:
    """Verification gate stub: replays a scripted list of verdicts.

    The loop must consult this on every completion claim (I1), so `calls`
    doubles as the assertion that the gate ran independently of anything the
    model said about its own work.
    """

    def __init__(self, verdicts: list[Verdict]) -> None:
        self._verdicts = list(verdicts)
        self.calls = 0

    def verify(self) -> Verdict:
        self.calls += 1
        if self._verdicts:
            return self._verdicts.pop(0)
        return Verdict(passed=False, checks=[CheckResult("stub", False, "no verdict scripted")])
