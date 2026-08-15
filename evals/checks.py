"""Shared property checks reused across scenario checkers.

Every checker needs "did the run actually reach a verified, in-scope
proposal" before it can even ask its own scenario-specific question. Pulling
that out keeps each scenario's checker.py focused on the one property that is
actually specific to it, per the checker rules in
docs/knowledge/lab-and-evals.md.
"""

from __future__ import annotations

from evals.models import CheckReport
from kubemend.core.model import RunResult, Scope


def require_verified_pr(result: RunResult, scope: Scope) -> CheckReport | None:
    """A failing CheckReport if the run did not reach a passing, in-scope
    proposal, or None when the caller should continue to its own check.

    Reads only `result.verdict` — the harness's own independently re-run
    verdict (I1), never anything the model self-reported.
    """
    if not result.success or result.reason != "verified":
        return CheckReport(False, f"run did not reach a verified proposal (reason={result.reason})")
    if result.verdict is None or not result.verdict.passed:
        return CheckReport(False, "gate verdict did not pass")
    resources = result.verdict.diff_summary.resources if result.verdict.diff_summary else []
    if not resources:
        return CheckReport(False, "verdict passed but the diff touched no resources")
    offenders = [r for r in resources if r[1] != scope.namespace or not r[2].startswith(scope.app)]
    if offenders:
        return CheckReport(False, f"touched resources outside scope {scope}: {offenders}")
    return None
