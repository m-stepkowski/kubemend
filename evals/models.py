"""Scenario data model (ARCHITECTURE.md §7, docs/knowledge/lab-and-evals.md).

The dataclasses a scenario.yaml deserializes into, and the report a checker
returns. Kept separate from scenario.py (the loader) and lab.py (probe dispatch
plus git/cluster operations) so each stays testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from kubemend.core.model import Scope

ProbeKind = Literal[
    "pod_waiting_reason",
    "pod_terminated_reason",
    "pod_condition",
    "event_reason",
    "log_contains",
]

ExpectedOutcome = Literal["pr", "handoff"]


@dataclass(frozen=True)
class SymptomProbe:
    """A poll-until-true check the runner waits on before invoking the agent.

    One generic shape covers every v0.1 scenario rather than a probe class per
    scenario. `kind` selects which piece of cluster state to look at; `value`
    is what must appear in it. `condition_type` is only meaningful for
    `pod_condition` (e.g. kind=pod_condition, condition_type=Ready, value=False).
    """

    kind: ProbeKind
    value: str
    condition_type: str = ""
    timeout_s: float = 120.0
    poll_interval_s: float = 3.0


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    title: str
    scope: Scope
    task_prompt: str
    expected_outcome: ExpectedOutcome
    symptom_probe: SymptomProbe
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CheckReport:
    """What a checker returns (docs/knowledge/lab-and-evals.md: checker rules).

    `passed` is a property-level verdict the checker computes independently of
    the gate. `detail` must say which property failed and what was observed —
    checker output is triage material, not a bare pass/fail bit.
    """

    passed: bool
    detail: str
