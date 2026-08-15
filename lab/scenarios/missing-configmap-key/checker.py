"""missing-configmap-key checker (docs/knowledge/lab-and-evals.md).

Property: the FEATURE_FLAGS *key* exists in config. The deployment template
requires this specific key via configMapKeyRef (not envFrom, which silently
tolerates a missing key) — see lab/gitops/apps/shop-api/templates/deployment.yaml.
configMapKeyRef only cares that the key is present; an empty string is a
completely valid value and starts the container fine, so this must not demand
non-empty — a first sweep caught exactly that false negative: three runs that
diagnosed and fixed the fault correctly (key restored, gate passed) were
marked failed here for writing FEATURE_FLAGS: "" instead of a non-empty value.
"""

from __future__ import annotations

import yaml

from evals.checks import require_verified_pr
from evals.lab import LabHandle
from evals.models import CheckReport
from kubemend.core.model import RunResult, Scope

SCOPE = Scope(namespace="shop", app="shop-api")


def check(result: RunResult, lab: LabHandle) -> CheckReport:
    if (failure := require_verified_pr(result, SCOPE)) is not None:
        return failure

    try:
        values = yaml.safe_load(lab.read_file("apps/shop-api/values.yaml"))
    except FileNotFoundError as exc:
        return CheckReport(False, str(exc))

    config = values.get("config") or {}
    if "FEATURE_FLAGS" not in config:
        return CheckReport(False, "config.FEATURE_FLAGS key is still missing")
    return CheckReport(True, f"config.FEATURE_FLAGS key restored: {config['FEATURE_FLAGS']!r}")
