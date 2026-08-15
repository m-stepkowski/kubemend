"""bad-env-endpoint checker (docs/knowledge/lab-and-evals.md).

Property: UPSTREAM_URL points back at shop-worker's real service and port,
not merely "differs from the broken value" — pointing it at something else
that happens to resolve would pass a weaker check without actually fixing
anything.
"""

from __future__ import annotations

import re

import yaml

from evals.checks import require_verified_pr
from evals.lab import LabHandle
from evals.models import CheckReport
from kubemend.core.model import RunResult, Scope

SCOPE = Scope(namespace="shop", app="shop-api")
# shop-worker's real service port (apps/shop-worker/values.yaml: service.port).
EXPECTED = re.compile(r"shop-worker(\.shop)?(\.svc(\.cluster\.local)?)?:9090$")


def check(result: RunResult, lab: LabHandle) -> CheckReport:
    if (failure := require_verified_pr(result, SCOPE)) is not None:
        return failure

    try:
        values = yaml.safe_load(lab.read_file("apps/shop-api/values.yaml"))
    except FileNotFoundError as exc:
        return CheckReport(False, str(exc))

    url = str((values.get("env") or {}).get("UPSTREAM_URL", ""))
    if not EXPECTED.search(url):
        return CheckReport(False, f"UPSTREAM_URL does not point at shop-worker:9090: {url!r}")
    return CheckReport(True, f"UPSTREAM_URL restored to {url!r}")
