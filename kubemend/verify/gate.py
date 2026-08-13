"""Independent verification (ARCHITECTURE.md §5, invariant I1).

Runs the validation pipeline fresh at termination and returns the Verdict. A
result the model produced by calling `validate_change` itself is a hint for the
model and never an input here — the gate re-runs regardless.

Failures re-enter context verbatim and check-by-check, because specificity
("kyverno: disallow-privileged FAILED on Deployment/shop/api: ...") is what
makes the retry loop converge where a generic failure stalls it.

The scope check lives on this side of the boundary and its implementation is
never surfaced to the model beyond pass/fail plus the offending resource — the
model should satisfy scope, not learn to game the checker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from kubemend.core.model import CheckResult, Verdict
from kubemend.tools.base import ToolSpec
from kubemend.tools.gitops.proposer import Proposer
from kubemend.tools.gitops.validator import Validator

NO_PROPOSAL = CheckResult(
    name="proposal",
    passed=False,
    detail=(
        "no_active_proposal: nothing has been proposed this run, so there is "
        "nothing to verify. Use propose_git_change first, or explain why the "
        "fix cannot be expressed in values files."
    ),
)


class VerificationGate(Protocol):
    """The single authority on whether a run succeeded.

    Deliberately argument-free: the gate resolves the active proposal branch
    itself rather than being handed one, so nothing the model produced can
    influence what gets validated.
    """

    def verify(self) -> Verdict: ...


@dataclass
class PipelineGate:
    """The real gate: re-runs the §5 pipeline over the run's active branch.

    It takes the proposer and validator as collaborators rather than a verdict,
    which is the structural expression of I1 — there is no parameter through
    which a model-supplied result could reach this class.
    """

    proposer: Proposer
    validator: Validator

    def verify(self) -> Verdict:
        branch = self.proposer.current_branch()
        if branch is None:
            return Verdict(passed=False, checks=[NO_PROPOSAL])

        apps = sorted(_apps_touched(self.proposer.files_written))
        if not apps:
            return Verdict(
                passed=False,
                checks=[
                    CheckResult(
                        name="proposal",
                        passed=False,
                        detail=(
                            "the proposed files are not under apps/<name>/, so no chart "
                            "could be identified to render"
                        ),
                    )
                ],
            )
        return self.validator.validate(apps)


def validate_tool_spec(gate: PipelineGate) -> ToolSpec:
    """`validate_change` as the model sees it (docs/knowledge/tool-contracts.md).

    This runs the same pipeline the gate runs, and that is fine: the result is a
    hint the model can act on mid-loop, never an input to termination. The gate
    re-runs independently when the model stops calling tools (I1), so a stale or
    lucky result here cannot end a run.
    """

    def _execute(_args: dict[str, Any]) -> dict[str, Any]:
        verdict = gate.verify()
        return {
            "passed": verdict.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in verdict.checks
            ],
            "diff_summary": (
                [list(r) for r in verdict.diff_summary.resources] if verdict.diff_summary else None
            ),
        }

    return ToolSpec(
        name="validate_change",
        description=(
            "Validate the current proposal branch: helm render, Kyverno policy check, "
            "live diff, and scope check. Returns per-check pass/fail with details. Use "
            "this to self-check before declaring the task done; the harness will re-run "
            "it independently anyway."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        executor=_execute,
        tier="verify",
        timeout_s=120.0,
    )


def _apps_touched(paths: list[str]) -> set[str]:
    """Map `apps/<name>/values*.yaml` back to the chart that must be rendered."""
    apps = set()
    for path in paths:
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "apps":
            apps.add(parts[1])
    return apps
