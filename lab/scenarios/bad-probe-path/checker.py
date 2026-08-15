"""bad-probe-path checker (docs/knowledge/lab-and-evals.md).

Property: the readiness path is a real path and not the injected 404. Does
not require it to be exactly "/" — any path shop-api actually serves is a
valid fix.
"""

from __future__ import annotations

import yaml

from evals.checks import require_verified_pr
from evals.lab import LabHandle
from evals.models import CheckReport
from kubemend.core.model import RunResult, Scope

SCOPE = Scope(namespace="shop", app="shop-api")
BROKEN_PATH = "/healthz-does-not-exist"


def check(result: RunResult, lab: LabHandle) -> CheckReport:
    if (failure := require_verified_pr(result, SCOPE)) is not None:
        return failure

    try:
        values = yaml.safe_load(lab.read_file("apps/shop-api/values.yaml"))
    except FileNotFoundError as exc:
        return CheckReport(False, str(exc))

    path = (values.get("probes") or {}).get("readiness", {}).get("path")
    if not path or not str(path).startswith("/"):
        return CheckReport(False, f"probes.readiness.path is not a valid path: {path!r}")
    if path == BROKEN_PATH:
        return CheckReport(False, f"probes.readiness.path is still the broken path {path!r}")
    return CheckReport(True, f"probes.readiness.path changed to {path!r}")
