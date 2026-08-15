"""bad-image-tag checker (docs/knowledge/lab-and-evals.md).

Property, not diff-equality: any tag that resolves in the registry is an
acceptable fix, not specifically the original "1.27-alpine".
"""

from __future__ import annotations

import yaml

from evals.checks import require_verified_pr
from evals.lab import LabHandle
from evals.models import CheckReport
from kubemend.core.model import RunResult, Scope

SCOPE = Scope(namespace="shop", app="shop-api")
BROKEN_TAG = "1.27-alpine-nonexistent"


def check(result: RunResult, lab: LabHandle) -> CheckReport:
    if (failure := require_verified_pr(result, SCOPE)) is not None:
        return failure

    try:
        values = yaml.safe_load(lab.read_file("apps/shop-api/values.yaml"))
    except FileNotFoundError as exc:
        return CheckReport(False, str(exc))

    tag = (values.get("image") or {}).get("tag")
    if not tag:
        return CheckReport(False, "proposed values.yaml has no image.tag")
    if tag == BROKEN_TAG:
        return CheckReport(False, f"image.tag is still the broken value {BROKEN_TAG!r}")
    return CheckReport(True, f"image.tag changed to {tag!r}")
