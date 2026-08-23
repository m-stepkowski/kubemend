"""Validation pipeline failure modes (ARCHITECTURE.md §5).

The M3 acceptance list names four: render error, policy violation, empty diff,
out-of-scope diff. Each must fail with the correct check name and a detail
string specific enough for the model to act on — "validation failed" measurably
stalls the retry loop where a named rule and resource converges it.

Driven entirely from fixtures. The stages are pure functions over command
output, so none of this needs helm, kyverno, or a cluster.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from kubemend.config import ValuesRepoSpec
from kubemend.core.model import Scope
from kubemend.tools.gitops.validator import (
    CommandResult,
    Validator,
    out_of_scope,
    parse_resources,
)

SCOPE = Scope(namespace="shop", app="shop-api")

KUBECTL_DIFF = """\
diff -u -N /tmp/LIVE-1/apps.v1.Deployment.shop.shop-api /tmp/MERGED-1/apps.v1.Deployment.shop.shop-api
--- /tmp/LIVE-1/apps.v1.Deployment.shop.shop-api
+++ /tmp/MERGED-1/apps.v1.Deployment.shop.shop-api
@@ -20,7 +20,7 @@
-          memory: 128Mi
+          memory: 512Mi
"""

KUBECTL_DIFF_OUT_OF_SCOPE = (
    KUBECTL_DIFF
    + """\
diff -u -N /tmp/LIVE-1/apps.v1.Deployment.payments.payments-api /tmp/MERGED-1/apps.v1.Deployment.payments.payments-api
@@ -1,1 +1,1 @@
-  replicas: 2
+  replicas: 5
"""
)

ARGOCD_DIFF = """\
===== apps/Deployment shop/shop-api ======
< replicas: 2
> replicas: 3
"""

KYVERNO_FAILURE = """\
Applying 5 policy rule(s) to 1 resource(s)...
policy disallow-latest-tag -> resource shop/Deployment/shop-api failed:
  disallow-latest: validation error: Using the :latest tag is not allowed; pin a specific version.
pass: 4, fail: 1, warn: 0, error: 0, skip: 0
"""


class ScriptedRunner:
    """Replays canned command results, keyed by the binary being invoked."""

    def __init__(self, **results: CommandResult) -> None:
        self._results = results
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []

    def run(
        self,
        cmd: Sequence[str],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(list(cmd))
        self.envs.append(dict(env) if env else None)
        for key, result in self._results.items():
            if key in cmd[0]:
                return result
        return CommandResult(0, stdout="")


def _validator(runner: ScriptedRunner, tmp_path: Path) -> Validator:
    return Validator(
        repo_path=tmp_path,
        scope=SCOPE,
        helm_bin=Path("/pinned/helm"),
        kyverno_bin=Path("/pinned/kyverno"),
        kubectl_bin=Path("/pinned/kubectl"),
        policies_dir=Path("/repo/policies"),
        runner=runner,
    )


# -- failure mode 1: render error ----------------------------------------


def test_render_error_fails_with_helm_stderr(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        helm=CommandResult(
            1,
            stderr='Error: template: shop-api/templates/deployment.yaml:12:14: executing "..." at <.Values.resources.limits>: nil pointer evaluating interface {}.limits',
        )
    )

    verdict = _validator(runner, tmp_path).validate(["shop-api"])

    assert verdict.passed is False
    assert verdict.checks[0].name == "helm_template"
    assert "nil pointer" in verdict.checks[0].detail, "the model needs helm's own message"
    assert len(verdict.checks) == 1, "a failed render short-circuits the rest"


def test_pinned_helm_binary_is_used_not_path(tmp_path: Path) -> None:
    """CLAUDE.md forbids PATH binaries; the PATH helm here is a major version ahead."""
    runner = ScriptedRunner(helm=CommandResult(0, stdout="kind: Deployment\n"))

    _validator(runner, tmp_path).validate(["shop-api"])

    assert runner.calls[0][0] == "/pinned/helm"


# -- failure mode 2: policy violation ------------------------------------


def test_policy_violation_names_the_failing_rule(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout="kind: Deployment\n"),
        kyverno=CommandResult(1, stdout=KYVERNO_FAILURE),
    )

    verdict = _validator(runner, tmp_path).validate(["shop-api"])

    assert verdict.passed is False
    kyverno = verdict.checks[-1]
    assert kyverno.name == "kyverno"
    assert "disallow-latest" in kyverno.detail
    assert "shop-api" in kyverno.detail


# -- failure mode 3: empty diff ------------------------------------------


def test_empty_diff_fails_as_no_effective_change(tmp_path: Path) -> None:
    """Catches the model 'fixing' a value by rewriting it to itself."""
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout="kind: Deployment\n"),
        kyverno=CommandResult(0, stdout="pass: 5, fail: 0"),
        kubectl=CommandResult(0, stdout="   \n"),
    )

    verdict = _validator(runner, tmp_path).validate(["shop-api"])

    assert verdict.passed is False
    assert verdict.checks[-1].name == "diff"
    assert "no_effective_change" in verdict.checks[-1].detail


# -- failure mode 4: out-of-scope diff -----------------------------------


def test_out_of_scope_diff_names_the_offending_resource(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout="kind: Deployment\n"),
        kyverno=CommandResult(0, stdout="pass: 5, fail: 0"),
        kubectl=CommandResult(1, stdout=KUBECTL_DIFF_OUT_OF_SCOPE),
    )

    verdict = _validator(runner, tmp_path).validate(["shop-api"])

    assert verdict.passed is False
    scope = verdict.checks[-1]
    assert scope.name == "scope"
    assert "payments" in scope.detail, "the offending resource must be named"
    assert "shop-api" in scope.detail


# -- the happy path -------------------------------------------------------


def test_all_stages_passing_yields_a_passed_verdict_with_a_diff_summary(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout="kind: Deployment\n"),
        kyverno=CommandResult(0, stdout="pass: 5, fail: 0, warn: 0"),
        kubectl=CommandResult(1, stdout=KUBECTL_DIFF),
    )

    verdict = _validator(runner, tmp_path).validate(["shop-api"])

    assert verdict.passed is True
    assert [c.name for c in verdict.checks] == [
        "helm_template",
        "kyverno",
        "diff",
        "scope",
        "quota",
    ]
    assert verdict.diff_summary is not None
    assert verdict.diff_summary.resources == [("Deployment", "shop", "shop-api")]


# -- diff parsing ---------------------------------------------------------


def test_both_diff_dialects_parse_to_the_same_triples() -> None:
    """The scope check must not depend on which diff tool was available."""
    assert parse_resources(KUBECTL_DIFF) == [("Deployment", "shop", "shop-api")]
    assert parse_resources(ARGOCD_DIFF) == [("Deployment", "shop", "shop-api")]


@pytest.mark.parametrize(
    "resource,expected_offender",
    [
        (("Deployment", "shop", "shop-api"), False),
        (("ConfigMap", "shop", "shop-api-config"), False),
        (("Deployment", "shop", "shop-worker"), True),
        (("Deployment", "payments", "shop-api"), True),
    ],
)
def test_scope_covers_owned_resources_but_not_siblings(
    resource: tuple[str, str, str], expected_offender: bool
) -> None:
    assert bool(out_of_scope([resource], SCOPE)) is expected_offender


# -- harness faults found by the first lab runs ---------------------------


def test_render_declares_the_scope_namespace(tmp_path: Path) -> None:
    """Namespaced policies match nothing against a manifest with no namespace.

    The first lab run reported "pass: 0, fail: 0" — a green Kyverno stage that
    had evaluated no rule at all, because `helm template` without --namespace
    renders metadata the policy pack's namespace selector cannot match.
    """
    runner = ScriptedRunner(helm=CommandResult(0, stdout="kind: Deployment\n"))

    _validator(runner, tmp_path).validate(["shop-api"])

    helm_cmd = runner.calls[0]
    assert "--namespace" in helm_cmd
    assert helm_cmd[helm_cmd.index("--namespace") + 1] == SCOPE.namespace


def test_policy_stage_fails_closed_when_no_rule_was_applied(tmp_path: Path) -> None:
    """A pass computed from zero applied rules is not a policy check."""
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout="kind: Deployment\n"),
        kyverno=CommandResult(0, stdout="pass: 0, fail: 0, warn: 0, error: 0, skip: 0\n"),
    )

    verdict = _validator(runner, tmp_path).validate(["shop-api"])

    assert verdict.passed is False
    policy = next(c for c in verdict.checks if c.name == "kyverno")
    assert policy.passed is False
    assert "no_policies_applied" in policy.detail
    assert "harness fault" in policy.detail, "the model must not retry its way out of this"


def test_diff_targets_the_rendered_manifests_not_the_repository(tmp_path: Path) -> None:
    """kubectl diff -f <repo dir> reads the README and dies on its extension."""
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout="kind: Deployment\n"),
        kyverno=CommandResult(0, stdout="pass: 5, fail: 0, warn: 0, error: 0, skip: 0\n"),
        kubectl=CommandResult(1, stdout=KUBECTL_DIFF),
    )

    _validator(runner, tmp_path).validate(["shop-api"])

    kubectl_cmd = next(c for c in runner.calls if c[0] == "/pinned/kubectl")
    target = Path(kubectl_cmd[kubectl_cmd.index("-f") + 1])
    assert target != tmp_path
    assert target.suffix in (".yaml", ".yml")


def test_diff_manifest_file_is_removed_even_when_kubectl_fails(tmp_path: Path) -> None:
    """The repo is a git workspace; a stray file would pollute the next proposal."""
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout="kind: Deployment\n"),
        kyverno=CommandResult(0, stdout="pass: 5, fail: 0, warn: 0, error: 0, skip: 0\n"),
        kubectl=CommandResult(2, stderr="the server could not find the requested resource"),
    )

    _validator(runner, tmp_path).validate(["shop-api"])

    assert list(tmp_path.glob(".kubemend-*.yaml")) == []


# -- diff via Argo CD (the §5 primary) ------------------------------------

ARGOCD_BIN = Path("/pinned/argocd")


def _argo_validator(runner: ScriptedRunner, tmp_path: Path) -> Validator:
    return Validator(
        repo_path=tmp_path,
        scope=SCOPE,
        helm_bin=Path("/pinned/helm"),
        kyverno_bin=Path("/pinned/kyverno"),
        kubectl_bin=Path("/pinned/kubectl"),
        policies_dir=Path("/repo/policies"),
        argocd_bin=ARGOCD_BIN,
        argocd_server="localhost:8080",
        argocd_token="jwt-token",
        runner=runner,
    )


def _rendering_runner(**extra: CommandResult) -> ScriptedRunner:
    return ScriptedRunner(
        helm=CommandResult(0, stdout="kind: Deployment\n"),
        kyverno=CommandResult(0, stdout="pass: 6, fail: 0, warn: 0, error: 0, skip: 0\n"),
        **extra,
    )


def test_argocd_is_preferred_over_kubectl_when_a_token_is_present(tmp_path: Path) -> None:
    """kubectl diff is a dry-run apply, which the read-only SA cannot perform."""
    runner = _rendering_runner(argocd=CommandResult(1, stdout=ARGOCD_DIFF))

    verdict = _argo_validator(runner, tmp_path).validate(["shop-api"])

    assert verdict.passed is True
    assert any(c[0] == str(ARGOCD_BIN) for c in runner.calls)
    assert not any(c[0] == "/pinned/kubectl" for c in runner.calls)


def test_argocd_diff_runs_with_path_pinned_to_the_managed_binaries(tmp_path: Path) -> None:
    """argocd shells out to `helm` from PATH; an unpinned one renders differently."""
    runner = _rendering_runner(argocd=CommandResult(1, stdout=ARGOCD_DIFF))

    _argo_validator(runner, tmp_path).validate(["shop-api"])

    index = next(i for i, c in enumerate(runner.calls) if c[0] == str(ARGOCD_BIN))
    env = runner.envs[index]
    assert env is not None
    assert env["PATH"].split(os.pathsep)[0] == "/pinned"


def test_argocd_exit_one_means_differences_not_failure(tmp_path: Path) -> None:
    runner = _rendering_runner(argocd=CommandResult(1, stdout=ARGOCD_DIFF))

    verdict = _argo_validator(runner, tmp_path).validate(["shop-api"])

    diff = next(c for c in verdict.checks if c.name == "diff")
    assert diff.passed is True


def test_argocd_empty_diff_is_no_effective_change(tmp_path: Path) -> None:
    """Catches the model 'fixing' a value by rewriting it to itself."""
    runner = _rendering_runner(argocd=CommandResult(0, stdout=""))

    verdict = _argo_validator(runner, tmp_path).validate(["shop-api"])

    diff = next(c for c in verdict.checks if c.name == "diff")
    assert diff.passed is False
    assert "no_effective_change" in diff.detail


def test_argocd_transport_failure_is_reported_not_swallowed(tmp_path: Path) -> None:
    runner = _rendering_runner(
        argocd=CommandResult(20, stderr="rpc error: code = Unavailable desc = connection refused")
    )

    verdict = _argo_validator(runner, tmp_path).validate(["shop-api"])

    diff = next(c for c in verdict.checks if c.name == "diff")
    assert diff.passed is False
    assert "connection refused" in diff.detail


def test_argocd_token_never_appears_in_a_check_detail(tmp_path: Path) -> None:
    """The token is a CLI argument, so a naive error passthrough would leak it."""
    runner = _rendering_runner(argocd=CommandResult(20, stderr="failed: --auth-token jwt-token"))

    verdict = _argo_validator(runner, tmp_path).validate(["shop-api"])

    assert "jwt-token" not in json.dumps([c.detail for c in verdict.checks])


def test_without_a_token_the_kubectl_fallback_is_used(tmp_path: Path) -> None:
    runner = _rendering_runner(kubectl=CommandResult(1, stdout=KUBECTL_DIFF))

    verdict = _validator(runner, tmp_path).validate(["shop-api"])

    assert verdict.passed is True
    assert any(c[0] == "/pinned/kubectl" for c in runner.calls)


# -- quota headroom stage --------------------------------------------------
# render/policy/diff/scope can all pass on a replica count the live
# ResourceQuota would refuse; a live sweep caught exactly that gap (M4).


def _rendered_deployment(namespace: str = "shop", name: str = "shop-api", replicas: int = 3) -> str:
    return f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {namespace}
spec:
  replicas: {replicas}
"""


class FakeKube:
    """A KubeQuery double: canned resourcequotas plus a live replica count."""

    def __init__(
        self,
        quotas: list[dict[str, object]] | None = None,
        live_replicas: int | None = 2,
        live_spec_replicas: int | None = None,
    ) -> None:
        self.quotas = quotas or []
        self.live_replicas = live_replicas
        # Only set for the spec-vs-status regression test: a Deployment whose
        # *desired* count (spec.replicas) differs from what it actually owns
        # (status.replicas) — the exact shape of the broken state this stage
        # exists to diagnose.
        self.live_spec_replicas = live_spec_replicas
        self.list_resource_calls: list[tuple[str, str]] = []
        self.get_resource_calls: list[tuple[str, str, str]] = []

    def list_resource(
        self, kind: str, namespace: str, selector: str | None = None
    ) -> list[dict[str, object]]:
        self.list_resource_calls.append((kind, namespace))
        return self.quotas if kind == "resourcequota" else []

    def get_resource(self, kind: str, namespace: str, name: str) -> dict[str, object]:
        self.get_resource_calls.append((kind, namespace, name))
        if self.live_replicas is None:
            from kubemend.tools.base import ClientError

            raise ClientError(f"{kind}/{name} not found in namespace {namespace}")
        live: dict[str, object] = {"status": {"replicas": self.live_replicas}}
        if self.live_spec_replicas is not None:
            live["spec"] = {"replicas": self.live_spec_replicas}
        return live


def _quota_object(
    name: str = "shop-api-pods", hard_pods: int = 4, used_pods: int = 3
) -> dict[str, object]:
    return {
        "metadata": {"name": name},
        "spec": {"hard": {"pods": str(hard_pods)}},
        "status": {"used": {"pods": str(used_pods)}},
    }


def _quota_validator(runner: ScriptedRunner, tmp_path: Path, kube: FakeKube | None) -> Validator:
    return Validator(
        repo_path=tmp_path,
        scope=SCOPE,
        helm_bin=Path("/pinned/helm"),
        kyverno_bin=Path("/pinned/kyverno"),
        kubectl_bin=Path("/pinned/kubectl"),
        policies_dir=Path("/repo/policies"),
        kube=kube,
        runner=runner,
    )


def test_quota_check_is_skipped_when_no_kube_client_is_wired_in(tmp_path: Path) -> None:
    """Existing fixtures that never touch quota semantics need no dummy client."""
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout=_rendered_deployment(replicas=99)),
        kyverno=CommandResult(0, stdout="pass: 6, fail: 0, warn: 0, error: 0, skip: 0\n"),
        kubectl=CommandResult(1, stdout=KUBECTL_DIFF),
    )

    verdict = _quota_validator(runner, tmp_path, kube=None).validate(["shop-api"])

    assert verdict.passed is True
    quota = next(c for c in verdict.checks if c.name == "quota")
    assert quota.passed is True


def test_quota_check_passes_when_the_proposal_fits(tmp_path: Path) -> None:
    kube = FakeKube(quotas=[_quota_object(hard_pods=4, used_pods=3)], live_replicas=2)
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout=_rendered_deployment(replicas=3)),
        kyverno=CommandResult(0, stdout="pass: 6, fail: 0, warn: 0, error: 0, skip: 0\n"),
        kubectl=CommandResult(1, stdout=KUBECTL_DIFF),
    )

    verdict = _quota_validator(runner, tmp_path, kube=kube).validate(["shop-api"])

    # other usage = used(3) - live(2) = 1; projected = 1 + proposed(3) = 4 <= hard(4)
    assert verdict.passed is True
    quota = next(c for c in verdict.checks if c.name == "quota")
    assert quota.passed is True


def test_quota_check_fails_when_the_proposal_would_exceed_live_headroom(tmp_path: Path) -> None:
    """The exact case a live sweep caught: render/policy/diff/scope all pass,
    but the replica count the model chose does not actually fit."""
    kube = FakeKube(quotas=[_quota_object(hard_pods=4, used_pods=3)], live_replicas=2)
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout=_rendered_deployment(replicas=4)),
        kyverno=CommandResult(0, stdout="pass: 6, fail: 0, warn: 0, error: 0, skip: 0\n"),
        kubectl=CommandResult(1, stdout=KUBECTL_DIFF),
    )

    verdict = _quota_validator(runner, tmp_path, kube=kube).validate(["shop-api"])

    # other usage = used(3) - live(2) = 1; projected = 1 + proposed(4) = 5 > hard(4)
    assert verdict.passed is False
    quota = next(c for c in verdict.checks if c.name == "quota")
    assert quota.passed is False
    assert "shop-api-pods" in quota.detail
    assert "5" in quota.detail and "4" in quota.detail


def test_quota_check_accounts_for_other_workloads_sharing_the_quota(tmp_path: Path) -> None:
    """A namespace can hold more than one app against a shared quota — the
    check must not assume the quota belongs solely to the app being fixed."""
    # used=3 total in the namespace; shop-api itself only owns 1 of those live,
    # so some other workload already holds 2.
    kube = FakeKube(quotas=[_quota_object(hard_pods=4, used_pods=3)], live_replicas=1)
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout=_rendered_deployment(replicas=2)),
        kyverno=CommandResult(0, stdout="pass: 6, fail: 0, warn: 0, error: 0, skip: 0\n"),
        kubectl=CommandResult(1, stdout=KUBECTL_DIFF),
    )

    verdict = _quota_validator(runner, tmp_path, kube=kube).validate(["shop-api"])

    # other usage = used(3) - live(1) = 2; projected = 2 + proposed(2) = 4 <= hard(4)
    assert verdict.passed is True


def test_quota_check_ignores_a_quota_with_no_pods_key(tmp_path: Path) -> None:
    kube = FakeKube(
        quotas=[{"metadata": {"name": "cpu-only"}, "spec": {"hard": {"requests.cpu": "2"}}}],
    )
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout=_rendered_deployment(replicas=99)),
        kyverno=CommandResult(0, stdout="pass: 6, fail: 0, warn: 0, error: 0, skip: 0\n"),
        kubectl=CommandResult(1, stdout=KUBECTL_DIFF),
    )

    verdict = _quota_validator(runner, tmp_path, kube=kube).validate(["shop-api"])

    assert verdict.passed is True


def test_quota_check_passes_when_the_namespace_has_no_resourcequota(tmp_path: Path) -> None:
    kube = FakeKube(quotas=[])
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout=_rendered_deployment(replicas=99)),
        kyverno=CommandResult(0, stdout="pass: 6, fail: 0, warn: 0, error: 0, skip: 0\n"),
        kubectl=CommandResult(1, stdout=KUBECTL_DIFF),
    )

    verdict = _quota_validator(runner, tmp_path, kube=kube).validate(["shop-api"])

    assert verdict.passed is True


def test_quota_check_treats_a_never_live_resource_as_owning_zero_pods(tmp_path: Path) -> None:
    """First-ever proposal for an app that does not exist live yet: nothing of
    its own to subtract from the quota's current usage."""
    kube = FakeKube(quotas=[_quota_object(hard_pods=4, used_pods=1)], live_replicas=None)
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout=_rendered_deployment(replicas=3)),
        kyverno=CommandResult(0, stdout="pass: 6, fail: 0, warn: 0, error: 0, skip: 0\n"),
        kubectl=CommandResult(1, stdout=KUBECTL_DIFF),
    )

    verdict = _quota_validator(runner, tmp_path, kube=kube).validate(["shop-api"])

    # other usage = used(1) - 0 = 1; projected = 1 + proposed(3) = 4 <= hard(4)
    assert verdict.passed is True


def test_quota_check_uses_live_status_not_desired_spec(tmp_path: Path) -> None:
    """The exact bug a live regression sweep caught: a broken Deployment's
    spec.replicas is still the unfulfillable desired count (6, over quota),
    while status.replicas (3) is what the quota's `used` figure actually
    counts. Reading spec here inverts the math into a negative "other usage"
    and lets an over-quota proposal look like it fits."""
    kube = FakeKube(
        quotas=[_quota_object(hard_pods=4, used_pods=4)],
        live_replicas=3,  # status.replicas: what actually exists
        live_spec_replicas=6,  # spec.replicas: the still-broken desired count
    )
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout=_rendered_deployment(replicas=4)),
        kyverno=CommandResult(0, stdout="pass: 6, fail: 0, warn: 0, error: 0, skip: 0\n"),
        kubectl=CommandResult(1, stdout=KUBECTL_DIFF),
    )

    verdict = _quota_validator(runner, tmp_path, kube=kube).validate(["shop-api"])

    # other usage = used(4) - status.replicas(3) = 1; projected = 1 + 4 = 5 > hard(4)
    assert verdict.passed is False
    quota = next(c for c in verdict.checks if c.name == "quota")
    assert quota.passed is False


def test_quota_check_never_runs_when_scope_already_failed(tmp_path: Path) -> None:
    """Short-circuit is preserved: an out-of-scope diff must not spend a live
    quota lookup on a proposal that already fails for an unrelated reason."""
    kube = FakeKube(quotas=[_quota_object()])
    runner = ScriptedRunner(
        helm=CommandResult(0, stdout=_rendered_deployment(replicas=3)),
        kyverno=CommandResult(0, stdout="pass: 6, fail: 0, warn: 0, error: 0, skip: 0\n"),
        kubectl=CommandResult(1, stdout=KUBECTL_DIFF_OUT_OF_SCOPE),
    )

    verdict = _quota_validator(runner, tmp_path, kube=kube).validate(["shop-api"])

    assert verdict.passed is False
    assert not any(c.name == "quota" for c in verdict.checks)
    assert kube.list_resource_calls == []


# -- split mode (M11) ------------------------------------------------------


def _split_validator(
    runner: ScriptedRunner, tmp_path: Path, *, chart_dirs: dict[str, Path], run_id: str = "run1"
) -> Validator:
    return Validator(
        repo_path=tmp_path,
        scope=SCOPE,
        helm_bin=Path("/pinned/helm"),
        kyverno_bin=Path("/pinned/kyverno"),
        kubectl_bin=Path("/pinned/kubectl"),
        policies_dir=Path("/repo/policies"),
        argocd_bin=ARGOCD_BIN,
        argocd_server="localhost:8080",
        argocd_token="jwt-token",
        runner=runner,
        chart_dirs=chart_dirs,
        run_id=run_id,
    )


def test_split_mode_render_passes_an_explicit_values_flag(tmp_path: Path) -> None:
    """Single-repo mode relies on helm's implicit values.yaml pickup inside
    the chart dir; that pickup breaks once chart and values are different
    checkouts, so split mode must pass --values explicitly."""
    chart_dir = tmp_path / "chart-checkout" / "shop-api"
    runner = _rendering_runner(argocd=CommandResult(1, stdout=ARGOCD_DIFF))

    _split_validator(runner, tmp_path, chart_dirs={"shop-api": chart_dir}).validate(["shop-api"])

    helm_call = next(c for c in runner.calls if c[0] == "/pinned/helm")
    assert str(chart_dir) in helm_call
    assert "--values" in helm_call
    values_path = helm_call[helm_call.index("--values") + 1]
    assert values_path == str(tmp_path / "apps" / "shop-api" / "values.yaml")


def test_single_repo_mode_never_passes_a_values_flag(tmp_path: Path) -> None:
    """Regression guard: chart_dirs=None must render byte-for-byte like
    before M11 — no --values flag, chart dir is repo_path/apps/<app>."""
    runner = _rendering_runner(argocd=CommandResult(1, stdout=ARGOCD_DIFF))

    _argo_validator(runner, tmp_path).validate(["shop-api"])

    helm_call = next(c for c in runner.calls if c[0] == "/pinned/helm")
    assert "--values" not in helm_call
    assert str(tmp_path / "apps" / "shop-api") in helm_call


# -- per-repo layout (M12) -------------------------------------------------


def test_a_custom_app_dir_template_moves_where_split_mode_reads_values(tmp_path: Path) -> None:
    """M12's values repos are per-team, and two teams' repos genuinely differ
    in layout — the pre-M12 `apps/<app>/` was hardcoded in two places."""
    chart_dir = tmp_path / "chart-checkout" / "shop-api"
    runner = _rendering_runner(argocd=CommandResult(1, stdout=ARGOCD_DIFF))
    validator = _split_validator(runner, tmp_path, chart_dirs={"shop-api": chart_dir})
    validator = replace(validator, app_dir_template="environments/prod/{app}")

    validator.validate(["shop-api"])

    helm_call = next(c for c in runner.calls if c[0] == "/pinned/helm")
    values_path = helm_call[helm_call.index("--values") + 1]
    assert values_path == str(tmp_path / "environments" / "prod" / "shop-api" / "values.yaml")


def test_a_custom_app_dir_template_also_moves_the_single_repo_chart_dir(tmp_path: Path) -> None:
    """In single-repo mode the same directory holds the chart *and* its
    values, so one template governs both."""
    runner = _rendering_runner(argocd=CommandResult(1, stdout=ARGOCD_DIFF))
    validator = replace(_argo_validator(runner, tmp_path), app_dir_template="charts/{app}")

    validator.validate(["shop-api"])

    helm_call = next(c for c in runner.calls if c[0] == "/pinned/helm")
    assert str(tmp_path / "charts" / "shop-api") in helm_call


def test_a_template_without_the_app_placeholder_is_rejected_at_config_time() -> None:
    """Every app would resolve to one directory, and the validator would
    render one app against another's values — wrong, and silently so."""
    with pytest.raises(ValidationError, match=r"must contain '\{app\}'"):
        ValuesRepoSpec(url="https://git.corp/platform/values.git", app_dir_template="apps/shared")


def test_split_mode_render_fails_clearly_for_an_app_outside_scope(tmp_path: Path) -> None:
    """The model wrote apps/<other-app>/values.yaml. Single-repo mode would
    render it and let the scope check catch it; split mode never cloned that
    app's chart, so render fails first — the detail must read as scope, not
    as harness breakage."""
    runner = _rendering_runner(argocd=CommandResult(1, stdout=ARGOCD_DIFF))

    verdict = _split_validator(
        runner, tmp_path, chart_dirs={"shop-api": tmp_path / "chart"}
    ).validate(["payments-api"])

    assert verdict.passed is False
    render = next(c for c in verdict.checks if c.name == "helm_template")
    assert render.passed is False
    assert "no chart checkout for 'payments-api'" in render.detail
    assert "shop-api" in render.detail
    assert not any(c[0] == "/pinned/helm" for c in runner.calls), "must fail before shelling out"


def test_split_mode_diff_uses_revisions_not_local(tmp_path: Path) -> None:
    """The multi-source live Application doesn't support --local (confirmed
    by the M11 design doc's lab spike); split mode diffs a pushed revision of
    the values source instead."""
    runner = _rendering_runner(argocd=CommandResult(1, stdout=ARGOCD_DIFF))

    _split_validator(
        runner, tmp_path, chart_dirs={"shop-api": tmp_path / "chart"}, run_id="20260101-abcd"
    ).validate(["shop-api"])

    argocd_call = next(c for c in runner.calls if c[0] == str(ARGOCD_BIN))
    assert "--local" not in argocd_call
    assert "--revisions" in argocd_call
    assert argocd_call[argocd_call.index("--revisions") + 1] == "kubemend/20260101-abcd"
    assert "--source-positions" in argocd_call
    assert argocd_call[argocd_call.index("--source-positions") + 1] == "2"


def test_split_mode_without_argocd_has_no_kubectl_fallback(tmp_path: Path) -> None:
    """The kubectl --server-side fallback was evaluated and dropped (design
    doc §6/§11): split mode without an Argo CD identity is a wiring problem,
    not something to soft-degrade into a different diff mechanism."""
    runner = _rendering_runner()

    verdict = Validator(
        repo_path=tmp_path,
        scope=SCOPE,
        helm_bin=Path("/pinned/helm"),
        kyverno_bin=Path("/pinned/kyverno"),
        kubectl_bin=Path("/pinned/kubectl"),
        policies_dir=Path("/repo/policies"),
        runner=runner,
        chart_dirs={"shop-api": tmp_path / "chart"},
        run_id="run1",
    ).validate(["shop-api"])

    assert verdict.passed is False
    diff = next(c for c in verdict.checks if c.name == "diff")
    assert diff.passed is False
    assert "no kubectl fallback" in diff.detail
    assert not any(c[0] == "/pinned/kubectl" for c in runner.calls)
