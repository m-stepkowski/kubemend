"""oom-limit checker (docs/knowledge/lab-and-evals.md).

Property: the ballast leaves real headroom under the memory limit — either
lowering allocateMb or raising the limit satisfies it, so this does not pin
the fix to one specific field.
"""

from __future__ import annotations

import re

import yaml

from evals.checks import require_verified_pr
from evals.lab import LabHandle
from evals.models import CheckReport
from kubemend.core.model import RunResult, Scope

SCOPE = Scope(namespace="shop", app="shop-worker")

# Container overhead beyond the raw ballast allocation; a limit only exactly
# equal to allocateMb still OOMs in practice, so require real headroom.
MIN_HEADROOM_MB = 16


def _memory_to_mb(value: str) -> int:
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else 0


def check(result: RunResult, lab: LabHandle) -> CheckReport:
    if (failure := require_verified_pr(result, SCOPE)) is not None:
        return failure

    try:
        values = yaml.safe_load(lab.read_file("apps/shop-worker/values.yaml"))
    except FileNotFoundError as exc:
        return CheckReport(False, str(exc))

    allocate_mb = int((values.get("workload") or {}).get("allocateMb", 0))
    limit = str((values.get("resources") or {}).get("limits", {}).get("memory", "0Mi"))
    limit_mb = _memory_to_mb(limit)

    if limit_mb < allocate_mb + MIN_HEADROOM_MB:
        return CheckReport(
            False,
            f"allocateMb={allocate_mb} leaves < {MIN_HEADROOM_MB}Mi headroom under "
            f"limit {limit!r} — still likely to OOM",
        )
    return CheckReport(True, f"allocateMb={allocate_mb}, limit={limit!r}: safe margin")
