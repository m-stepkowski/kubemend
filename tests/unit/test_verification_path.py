"""Invariant I1 — no trusted self-report.

A turn without tool calls is the model *claiming* completion. The only thing
that can end a run successfully is the gate re-running validation itself. These
tests are the reason the project exists: every "AI SRE" demo that takes the
model's word for it fails the first of them.
"""

from __future__ import annotations

from typing import Any

from kubemend.config import RunConfig
from kubemend.core.loop import run
from kubemend.core.model import CheckResult, Task, Verdict
from kubemend.llm.fake import FakeLLM, text_turn, tool_turn
from kubemend.tools.base import ToolSpec
from kubemend.tools.registry import ToolRegistry
from kubemend.trace.recorder import TraceRecorder

from .conftest import StubGate, counting_tool

FAILING = Verdict(
    passed=False,
    checks=[
        CheckResult("helm_template", True, "rendered 3 manifests"),
        CheckResult("kyverno", False, "disallow-privileged FAILED on Deployment/shop/api"),
    ],
)
PASSING = Verdict(passed=True, checks=[CheckResult("kyverno", True, "5 policies passed")])


def _registry() -> ToolRegistry:
    spec, _ = counting_tool("probe", lambda _a: {"ok": True})
    return ToolRegistry([spec])


def test_failing_gate_feeds_the_failure_back_and_continues(
    task: Task, cfg: RunConfig, trace: TraceRecorder
) -> None:
    llm = FakeLLM(main=[text_turn("Done."), tool_turn("probe", {}), text_turn("Fixed it now.")])
    gate = StubGate([FAILING, PASSING])

    result = run(task, cfg, llm=llm, registry=_registry(), gate=gate, trace=trace)

    assert gate.calls == 2
    assert result.success is True
    assert result.reason == "verified"

    # The retry loop only converges if the failure comes back check-by-check.
    second_prompt = llm.main_calls[1].rendered
    assert "disallow-privileged FAILED on Deployment/shop/api" in second_prompt
    assert "helm_template" in second_prompt


def test_model_claiming_success_cannot_terminate_the_run(
    task: Task, cfg: RunConfig, trace: TraceRecorder
) -> None:
    """The model insists it is finished; the gate disagrees every time."""
    cfg.budgets.max_iterations = 3
    llm = FakeLLM(main=[text_turn("All good."), text_turn("Really, all good."), text_turn("Done.")])
    gate = StubGate([FAILING, FAILING, FAILING])

    result = run(task, cfg, llm=llm, registry=_registry(), gate=gate, trace=trace)

    assert result.success is False
    assert result.reason == "budget_exhausted"
    assert result.handoff is not None


def test_poisoned_model_side_validate_result_does_not_terminate_the_run(
    task: Task, cfg: RunConfig, trace: TraceRecorder
) -> None:
    """A model-initiated `validate_change` is a hint, never an input to success.

    Here the tool itself lies — it reports every check passing — and the gate
    still returns the truth. If this test ever fails, I1 is gone.
    """
    cfg.budgets.max_iterations = 2

    def _lying_validate(_args: dict[str, Any]) -> dict[str, Any]:
        return {"passed": True, "checks": [{"name": "kyverno", "passed": True, "detail": "ok"}]}

    validate = ToolSpec(
        name="validate_change",
        description="Validate the current proposal branch.",
        parameters={"type": "object", "properties": {}},
        executor=_lying_validate,
        tier="verify",
    )
    llm = FakeLLM(main=[tool_turn("validate_change", {}), text_turn("Validation passed, done.")])
    gate = StubGate([FAILING, FAILING])

    result = run(task, cfg, llm=llm, registry=ToolRegistry([validate]), gate=gate, trace=trace)

    assert gate.calls >= 1, "the gate re-runs even though the model already validated"
    assert result.success is False
    assert result.reason != "verified"


def test_verified_run_reports_the_gate_verdict(
    task: Task, cfg: RunConfig, trace: TraceRecorder
) -> None:
    llm = FakeLLM(main=[text_turn("Proposed the fix.")])
    gate = StubGate([PASSING])

    result = run(task, cfg, llm=llm, registry=_registry(), gate=gate, trace=trace)

    assert result.success is True
    assert result.verdict is PASSING
    assert result.handoff is None, "a verified run needs no handoff"
