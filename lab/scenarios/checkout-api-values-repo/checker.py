"""checkout-api-values-repo checker (M12 acceptance scenario).

Same property as bad-image-tag's checker — any tag that resolves in the
registry is an acceptable fix — but the point here is *which repo* the fix
lands in. checkout-api's values live in `kubemend/gitops-payments`, a second
values repo, while every other demo app's live in `kubemend/gitops`.

Two things are asserted beyond the fix itself, and they are what make this an
M12 test rather than a second bad-image-tag:

1. The PR was opened against `gitops-payments` — the *routed* repo.
2. `gitops` received no branch *for this run*. A silent write to the wrong
   repo is the failure mode routing exists to prevent, and it would otherwise
   look identical to success from the model's side.

Adversarially configured on purpose: `kubemend.multi-values.yaml` leaves the
top-level `gitops.gitea_owner`/`gitea_repo` pointing at `kubemend/gitops` —
the wrong repo. Only correct per-repo routing puts the PR in the right place,
so a regression that ignored `values_repos` would fail assertion 1 rather
than quietly passing.
"""

from __future__ import annotations

import subprocess

import yaml

from evals.checks import require_verified_pr
from evals.lab import LabHandle
from evals.models import CheckReport
from kubemend.core.model import RunResult, Scope

SCOPE = Scope(namespace="shop-payments", app="checkout-api")
BROKEN_TAG = "1.27-alpine-nonexistent"
ROUTED_REPO = "gitops-payments"
OTHER_REPO = "http://localhost:3000/kubemend/gitops.git"


def _other_repo_branches() -> list[str]:
    """Branch names in the repo this run must *not* have touched.

    `ls-remote` against gitea rather than a local checkout: the question is
    what the run actually pushed, and a local workspace could be stale or
    reset by the harness between the run and this check.
    """
    result = subprocess.run(
        ["git", "ls-remote", "--heads", OTHER_REPO],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return []
    return [line.split("refs/heads/")[-1] for line in result.stdout.splitlines() if line.strip()]


def _this_runs_branch(result: RunResult) -> str | None:
    """`Proposer.branch_name` for this run: `kubemend/<run_id>`, and the trace
    is written to `<run_id>.jsonl`, so its stem is the run id.

    Scoped to *this* run deliberately. Asserting the other repo holds no
    `kubemend/*` branch at all is wrong — it accumulates them from every
    previous run against it, and the first version of this checker failed all
    three iterations on branches left by the morning's M11 sweeps.
    """
    if result.trace_path is None:
        return None
    return f"kubemend/{result.trace_path.stem}"


def check(result: RunResult, lab: LabHandle) -> CheckReport:
    if (failure := require_verified_pr(result, SCOPE)) is not None:
        return failure

    if not result.pr_ref:
        return CheckReport(False, "run reported no PR reference")
    if ROUTED_REPO not in result.pr_ref:
        return CheckReport(
            False,
            f"PR opened against the wrong repo: {result.pr_ref!r} does not name {ROUTED_REPO!r}",
        )

    branch = _this_runs_branch(result)
    if branch is None:
        return CheckReport(False, "run reported no trace path, so its branch name is unknown")
    if branch in _other_repo_branches():
        return CheckReport(
            False, f"the run pushed {branch} to {OTHER_REPO} — it must only touch the routed repo"
        )

    try:
        values = yaml.safe_load(lab.read_file("apps/checkout-api/values.yaml"))
    except FileNotFoundError as exc:
        return CheckReport(False, str(exc))

    tag = (values.get("image") or {}).get("tag")
    if not tag:
        return CheckReport(False, "proposed values.yaml has no image.tag")
    if tag == BROKEN_TAG:
        return CheckReport(False, f"image.tag is still the broken value {BROKEN_TAG!r}")
    return CheckReport(True, f"image.tag changed to {tag!r} in the routed repo")
