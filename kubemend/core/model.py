"""Core data model (ARCHITECTURE.md §2.1).

Frozen dataclasses passed between the loop, the tool layer, and the gate:
ToolCall, ToolOutcome, CheckResult, DiffSummary, Verdict, HandoffReport, and the
mutable RunResult the CLI and eval runner consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

TerminationReason = Literal[
    "verified",
    "budget_exhausted",
    "loop_detected",
    "handoff",
    "fatal_error",
]

ToolTier = Literal["read", "propose", "verify"]

ModelTier = Literal["main", "cheap"]


@dataclass(frozen=True)
class Scope:
    """The blast radius a run is allowed to touch.

    The scope check in verify/gate.py is the enforcement point; this is the
    declaration the model is shown and the gate measures against.
    """

    namespace: str
    app: str


@dataclass(frozen=True)
class Task:
    """What the run was asked to do, plus the scope it may act within."""

    statement: str
    scope: Scope
    window: str = "-30m"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    """The result of one executor invocation, post-redaction and truncation.

    `raw_bytes` is the size *before* truncation, so a trace shows how much
    evidence was withheld from the model rather than only what it saw.
    """

    call_id: str
    ok: bool
    payload: dict[str, Any]
    truncated: bool
    raw_bytes: int
    duration_ms: int


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    # True when the check could not run because a *dependency* failed — an
    # expired Argo CD token, a refused connection — rather than because the
    # proposal was wrong (M14). Structured rather than sniffed from `detail`
    # because the eval runner excludes these iterations from the pass rate,
    # and a misread there silently inflates the number the whole harness
    # exists to keep honest. Never a reason to retry (CLAUDE.md): it is
    # flagged, not papered over.
    infra_error: bool = False


@dataclass(frozen=True)
class DiffSummary:
    """Resources a proposed change touches, as (kind, namespace, name)."""

    resources: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Verdict:
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    diff_summary: DiffSummary | None = None

    @property
    def infra_error(self) -> bool:
        """Did this verdict fail because a dependency was broken, rather than
        because the proposal was?

        The eval runner uses this to keep such an iteration out of the pass
        rate: a run where the model proposed the right fix and the gate could
        not check it is not a model failure, and counting it as one makes the
        sweep quietly pessimistic.
        """
        return any(check.infra_error for check in self.checks)


@dataclass(frozen=True)
class Hypothesis:
    statement: str
    confidence: float
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HandoffReport:
    """Structured findings when a run ends without a verified proposal (§2.6).

    A good handoff is a designed outcome, not an error path — `blocking_reason`
    (e.g. `fix_not_expressible_in_values`) is what makes the negative scenarios
    in M6 checkable.
    """

    root_cause_hypotheses: list[Hypothesis] = field(default_factory=list)
    what_was_ruled_out: list[str] = field(default_factory=list)
    suggested_next_steps: list[str] = field(default_factory=list)
    blocking_reason: str | None = None


@dataclass
class RunResult:
    success: bool
    reason: TerminationReason
    verdict: Verdict | None = None
    handoff: HandoffReport | None = None
    pr_ref: str | None = None
    cost_usd: float = 0.0
    iterations: int = 0
    wall_seconds: float = 0.0
    trace_path: Path | None = None
