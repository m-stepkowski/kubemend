"""shop-api-split-chart-repo checker (M11 acceptance scenario).

Same property as bad-image-tag's checker — any tag that resolves in the
registry is an acceptable fix — but this is the split-mode case: the app's
chart lives in a separate gitea repo (kubemend/shop-api-chart) from its
values (kubemend/gitops), so a passing run here proves the whole M11 path —
routing, the second chart reader, the validator's split-mode render, and the
multi-source `--revisions` diff — worked end to end against a real incident,
not just fixtures.
"""

from __future__ import annotations

import yaml

from evals.checks import require_verified_pr
from evals.lab import LabHandle
from evals.models import CheckReport
from kubemend.core.model import RunResult, Scope

SCOPE = Scope(namespace="shop-split", app="shop-api-split")
BROKEN_TAG = "1.27-alpine-nonexistent"


def check(result: RunResult, lab: LabHandle) -> CheckReport:
    if (failure := require_verified_pr(result, SCOPE)) is not None:
        return failure

    try:
        values = yaml.safe_load(lab.read_file("apps/shop-api-split/values.yaml"))
    except FileNotFoundError as exc:
        return CheckReport(False, str(exc))

    tag = (values.get("image") or {}).get("tag")
    if not tag:
        return CheckReport(False, "proposed values.yaml has no image.tag")
    if tag == BROKEN_TAG:
        return CheckReport(False, f"image.tag is still the broken value {BROKEN_TAG!r}")
    return CheckReport(True, f"image.tag changed to {tag!r}")
