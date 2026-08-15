"""quota-conflict checker (docs/knowledge/lab-and-evals.md).

Property, read from the *rendered* chart rather than the raw values file: the
fix has to keep replicaCount under the quota the chart itself defines
(apps/shop-api/templates/resourcequota.yaml), not merely "different from 6" —
and the quota is not agent-writable (it lives in templates/, outside
writable_globs), so re-rendering to get its current value is honest rather
than hardcoding it here.
"""

from __future__ import annotations

import yaml

from evals.checks import require_verified_pr
from evals.lab import LabHandle
from evals.models import CheckReport
from kubemend.core.model import RunResult, Scope

SCOPE = Scope(namespace="shop", app="shop-api")
BROKEN_REPLICAS = 6


def check(result: RunResult, lab: LabHandle) -> CheckReport:
    if (failure := require_verified_pr(result, SCOPE)) is not None:
        return failure

    try:
        rendered = lab.render("shop-api", SCOPE.namespace)
    except RuntimeError as exc:
        return CheckReport(False, str(exc))

    replicas: int | None = None
    max_pods: int | None = None
    for doc in yaml.safe_load_all(rendered):
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") == "Deployment":
            replicas = int(doc.get("spec", {}).get("replicas", 0))
        if doc.get("kind") == "ResourceQuota":
            max_pods = int(doc.get("spec", {}).get("hard", {}).get("pods", 0))

    if replicas is None or max_pods is None:
        return CheckReport(
            False, "could not find both Deployment.replicas and ResourceQuota.hard.pods"
        )
    if replicas == BROKEN_REPLICAS:
        return CheckReport(False, f"replicaCount is still the broken value {BROKEN_REPLICAS}")
    if not (1 <= replicas < max_pods):
        return CheckReport(
            False, f"replicaCount={replicas} does not fit under the quota (max_pods={max_pods})"
        )
    return CheckReport(True, f"replicaCount={replicas} fits under quota (max_pods={max_pods})")
