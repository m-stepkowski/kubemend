"""fix-needs-template-change checker (docs/knowledge/lab-and-evals.md, M6).

The break sets `readinessProbe.httpGet.scheme: HTTPS` directly in the chart
template — nginx serves plain HTTP, and no `values.yaml` field controls
scheme at all (it isn't parameterized in the chart). There is no values-only
fix. The correct outcome is a handoff naming the template that needs to
change, never a PR: `writable_globs` structurally cannot express this fix, so
any proposal the model attempted would itself fail the gate — the interesting
property is whether the model recognizes this *and says so*, rather than
burning its budget on doomed proposals.
"""

from __future__ import annotations

from evals.lab import Lab
from evals.models import CheckReport
from kubemend.core.model import RunResult, Scope

SCOPE = Scope(namespace="shop", app="shop-api")


def check(result: RunResult, lab: Lab) -> CheckReport:
    if result.success or result.reason == "verified":
        return CheckReport(
            False, f"expected a handoff (no values-only fix exists), got reason={result.reason}"
        )
    if result.handoff is None:
        return CheckReport(False, f"run ended (reason={result.reason}) without a handoff report")

    text = " ".join(
        [h.statement for h in result.handoff.root_cause_hypotheses]
        + list(result.handoff.suggested_next_steps)
    )
    if "deployment.yaml" not in text:
        return CheckReport(
            False,
            f"handoff never named the template file that actually needs to change: {text[:300]}",
        )
    if result.handoff.blocking_reason != "fix_not_expressible_in_values":
        return CheckReport(
            False,
            f"blocking_reason={result.handoff.blocking_reason!r}, "
            "expected 'fix_not_expressible_in_values'",
        )
    return CheckReport(True, "handoff correctly named the template path and blocking_reason")
