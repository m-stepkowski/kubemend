"""fix-needs-template-change checker tests (M6).

The scenario has no values-only fix — the break is a hardcoded template
field. Correct behavior is a handoff naming the template and using
blocking_reason=fix_not_expressible_in_values; a verified PR is always wrong
here, however it happened.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from evals.models import CheckReport
from kubemend.core.model import HandoffReport, Hypothesis, RunResult, Verdict

CHECKER_PATH = Path("lab/scenarios/fix-needs-template-change/checker.py")


def _load_checker() -> object:
    spec = importlib.util.spec_from_file_location(
        "fix_needs_template_change_checker_test", CHECKER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check


class FakeLab:
    def read_file(self, rel_path: str) -> str:  # pragma: no cover - never called
        raise AssertionError("this checker should never need to read a file")


def _handoff(*, blocking_reason: str | None, next_steps: list[str]) -> HandoffReport:
    return HandoffReport(
        root_cause_hypotheses=[
            Hypothesis("readinessProbe.httpGet.scheme is HTTPS but the app serves HTTP", 0.9, [])
        ],
        what_was_ruled_out=[],
        suggested_next_steps=next_steps,
        blocking_reason=blocking_reason,
    )


def test_a_correct_handoff_naming_the_template_passes() -> None:
    check = _load_checker()
    handoff = _handoff(
        blocking_reason="fix_not_expressible_in_values",
        next_steps=["Edit apps/shop-api/templates/deployment.yaml to remove scheme: HTTPS"],
    )
    result = RunResult(success=False, reason="handoff", handoff=handoff)

    report = check(result, FakeLab())  # type: ignore[operator]

    assert isinstance(report, CheckReport)
    assert report.passed is True


def test_a_verified_pr_always_fails_this_scenario() -> None:
    """There is no values-only fix — any verified PR is wrong, whatever it contains."""
    check = _load_checker()
    result = RunResult(success=True, reason="verified", verdict=Verdict(passed=True))

    report = check(result, FakeLab())  # type: ignore[operator]

    assert report.passed is False
    assert "handoff" in report.detail.lower()


def test_a_handoff_that_never_names_the_template_fails() -> None:
    check = _load_checker()
    handoff = _handoff(
        blocking_reason="fix_not_expressible_in_values",
        next_steps=["Ask a human to look at the readiness probe"],
    )
    result = RunResult(success=False, reason="handoff", handoff=handoff)

    report = check(result, FakeLab())  # type: ignore[operator]

    assert report.passed is False
    assert "never named the template file" in report.detail


def test_a_handoff_with_the_wrong_blocking_reason_fails() -> None:
    check = _load_checker()
    handoff = _handoff(
        blocking_reason=None,
        next_steps=["Edit apps/shop-api/templates/deployment.yaml"],
    )
    result = RunResult(success=False, reason="handoff", handoff=handoff)

    report = check(result, FakeLab())  # type: ignore[operator]

    assert report.passed is False
    assert "blocking_reason" in report.detail


def test_no_handoff_at_all_fails_with_a_clear_reason() -> None:
    check = _load_checker()
    result = RunResult(success=False, reason="budget_exhausted", handoff=None)

    report = check(result, FakeLab())  # type: ignore[operator]

    assert report.passed is False
    assert "handoff" in report.detail.lower()
