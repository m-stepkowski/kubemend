"""Validation pipeline failure modes (ARCHITECTURE.md §5).

The M3 acceptance list names four: render error, policy violation, empty diff,
out-of-scope diff. Each must fail with the correct check name and a detail
string specific enough for the model to act on — "validation failed" measurably
stalls the retry loop where a named rule and resource converges it.

Driven entirely from fixtures. The stages are pure functions over command
output, so none of this needs helm, kyverno, or a cluster.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

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

    def run(self, cmd: Sequence[str], cwd: Path | None = None) -> CommandResult:
        self.calls.append(list(cmd))
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
    assert [c.name for c in verdict.checks] == ["helm_template", "kyverno", "diff", "scope"]
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
