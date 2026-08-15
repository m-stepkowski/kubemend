"""Validation pipeline (ARCHITECTURE.md §5).

Five stages against the active branch's working tree: helm template, Kyverno
policy check against the project's own pack, live diff (argocd app diff, falling
back to kubectl diff --server-side), the harness-owned scope check, and a live
quota-headroom check. An empty diff fails as `no_effective_change` — it catches
the model "fixing" a value by rewriting it to itself.

Uses the Taskfile-pinned helm and kyverno binaries, never PATH: the PATH helm on
a developer machine was observed to be a full major version ahead of the pinned
one, which would render different manifests than CI.

Every stage is a pure function over command output, and the commands themselves
go through an injected runner. That split is what lets each failure mode in the
M3 acceptance list be driven from a fixture rather than a broken cluster.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from kubemend.core.model import CheckResult, DiffSummary, Scope, Verdict
from kubemend.tools.base import ToolError

# (kind, namespace, name)
Resource = tuple[str, str, str]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner(Protocol):
    def run(
        self,
        cmd: Sequence[str],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class KubeQuery(Protocol):
    def list_resource(
        self, kind: str, namespace: str, selector: str | None = None
    ) -> list[dict[str, Any]]: ...

    def get_resource(self, kind: str, namespace: str, name: str) -> dict[str, Any]: ...


class SubprocessRunner:
    """Real execution. Timeouts are per-command so one wedged binary cannot
    hold the run open past its wall-clock budget."""

    def __init__(self, timeout_s: float = 120.0) -> None:
        self.timeout_s = timeout_s

    def run(
        self,
        cmd: Sequence[str],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        try:
            proc = subprocess.run(
                list(cmd),
                cwd=str(cwd) if cwd else None,
                env={**os.environ, **env} if env else None,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(returncode=124, stderr=f"timed out after {self.timeout_s}s")
        except FileNotFoundError as exc:
            return CommandResult(returncode=127, stderr=str(exc))
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


# -- diff parsing ---------------------------------------------------------

# kubectl diff names its temp files <group>.<version>.<Kind>.<namespace>.<name>,
# which is the only structured thing in an otherwise free-form unified diff.
_KUBECTL_PATH = re.compile(
    r"^diff -u -N \S*/(?:[\w.-]+\.)??(?P<kind>[A-Z][\w]*)\.(?P<ns>[\w-]+)\.(?P<name>[\w.-]+)\s",
    re.M,
)

# argocd prints a header per resource before each hunk, qualifying the kind with
# its API group as `apps/Deployment` — hence `/` in the kind character class.
_ARGOCD_HEADER = re.compile(
    r"^===+\s*(?P<kind>[\w./]+?)\s+(?P<ns>[\w-]+)/(?P<name>[\w.-]+)\s*===+", re.M
)


def parse_resources(diff_text: str) -> list[Resource]:
    """Extract the (kind, namespace, name) triples a diff touches.

    Tried in both dialects because the pipeline prefers `argocd app diff` and
    falls back to `kubectl diff`; the scope check must behave identically either
    way or the control depends on which tool happened to be available.
    """
    found: list[Resource] = []
    for pattern in (_ARGOCD_HEADER, _KUBECTL_PATH):
        for match in pattern.finditer(diff_text):
            # Both dialects qualify the kind — `apps/Deployment` (argocd) and
            # `apps.v1.Deployment` (kubectl) — so reduce to the bare Kind.
            kind = re.split(r"[./]", match.group("kind"))[-1]
            triple = (kind, match.group("ns"), match.group("name"))
            if triple not in found:
                found.append(triple)
    return found


def out_of_scope(resources: Sequence[Resource], scope: Scope) -> list[Resource]:
    """Resources outside the declared namespace, or not belonging to the app.

    Name matching is prefix-based because a Deployment `shop-api` owns
    `shop-api-7d9f`, `shop-api-config`, and so on. The implementation is never
    surfaced to the model beyond pass/fail and the offending resource — it
    should satisfy scope, not learn to game the checker.
    """
    offenders = []
    for kind, namespace, name in resources:
        if namespace != scope.namespace or not name.startswith(scope.app):
            offenders.append((kind, namespace, name))
    return offenders


# -- stages ---------------------------------------------------------------


@dataclass
class Validator:
    repo_path: Path
    scope: Scope
    helm_bin: Path
    kyverno_bin: Path
    kubectl_bin: Path
    policies_dir: Path
    kube_version: str = "1.31.2"
    # Argo CD is the primary diff engine (§5). Without a token the stage falls
    # back to kubectl, which only works against a cluster identity permitted to
    # dry-run apply — not the read-only ServiceAccount a normal run holds.
    argocd_bin: Path | None = None
    argocd_server: str = ""
    argocd_token: str = ""
    argocd_plaintext: bool = True
    # Read-only: only list_resource/get_resource, the same surface the agent's
    # own get_k8s_state tool uses. None skips the quota stage (it passes
    # automatically) so existing fixtures that never touch quota semantics
    # need no dummy client.
    kube: KubeQuery | None = None
    runner: CommandRunner = field(default_factory=SubprocessRunner)

    @property
    def _uses_argocd(self) -> bool:
        return bool(self.argocd_bin and self.argocd_token)

    def validate(self, apps: Sequence[str]) -> Verdict:
        """Run the five stages, stopping at the first failure.

        Short-circuiting is deliberate: a policy result computed from manifests
        that did not render is noise, and the model only needs the first real
        reason.
        """
        checks: list[CheckResult] = []

        rendered, render_check = self._render(apps)
        checks.append(render_check)
        if not render_check.passed:
            return Verdict(passed=False, checks=checks)

        policy_check = self._policy(rendered)
        checks.append(policy_check)
        if not policy_check.passed:
            return Verdict(passed=False, checks=checks)

        diff_text, diff_check = self._diff(rendered, apps)
        checks.append(diff_check)
        if not diff_check.passed:
            return Verdict(passed=False, checks=checks)

        resources = parse_resources(diff_text)
        scope_check = self._scope(resources)
        checks.append(scope_check)
        if not scope_check.passed:
            return Verdict(
                passed=False, checks=checks, diff_summary=DiffSummary(resources=list(resources))
            )

        quota_check = self._quota(rendered)
        checks.append(quota_check)
        return Verdict(
            passed=quota_check.passed,
            checks=checks,
            diff_summary=DiffSummary(resources=list(resources)),
        )

    def _render(self, apps: Sequence[str]) -> tuple[str, CheckResult]:
        manifests: list[str] = []
        for app in apps:
            chart = self.repo_path / "apps" / app
            result = self.runner.run(
                [
                    str(self.helm_bin),
                    "template",
                    app,
                    str(chart),
                    "--namespace",
                    self.scope.namespace,
                    "--kube-version",
                    self.kube_version,
                ],
                cwd=self.repo_path,
            )
            if not result.ok:
                return "", CheckResult(
                    name="helm_template",
                    passed=False,
                    detail=f"{app}: {result.stderr.strip()[:400]}",
                )
            manifests.append(result.stdout)
        return "\n---\n".join(manifests), CheckResult(
            "helm_template", True, f"rendered {len(apps)} app(s)"
        )

    def _policy(self, rendered: str) -> CheckResult:
        manifest_file = self.repo_path / ".kubemend-rendered.yaml"
        manifest_file.write_text(rendered)
        try:
            result = self.runner.run(
                [
                    str(self.kyverno_bin),
                    "apply",
                    str(self.policies_dir),
                    "--resource",
                    str(manifest_file),
                    "--audit-warn=false",
                ],
                cwd=self.repo_path,
            )
        finally:
            manifest_file.unlink(missing_ok=True)

        if not result.ok:
            return CheckResult("kyverno", False, _policy_failures(result.stdout, result.stderr))
        if _policy_evaluated(result.stdout) == 0:
            # Fail closed. A pass computed from zero applied rules is not a
            # policy check, and it is exactly the shape a namespace mismatch
            # takes — the gate would wave a violating manifest straight through.
            return CheckResult(
                name="kyverno",
                passed=False,
                detail=(
                    "no_policies_applied: the policy pack matched no rendered resource, so "
                    "nothing was actually checked. This is a harness fault, not a fault in "
                    "the proposal."
                ),
            )
        return CheckResult("kyverno", True, _policy_summary(result.stdout))

    def _diff(self, rendered: str, apps: Sequence[str]) -> tuple[str, CheckResult]:
        if self._uses_argocd:
            return self._argocd_diff(apps)
        return self._kubectl_diff(rendered)

    def _argocd_diff(self, apps: Sequence[str]) -> tuple[str, CheckResult]:
        """Diff each app's local chart against what Argo has live.

        Argo shells out to `helm` from PATH, so PATH is pinned to the directory
        holding the Taskfile-managed binaries — otherwise the gate would render
        with whatever helm the developer happens to have installed and validate
        manifests Argo would never produce.
        """
        env = {"PATH": f"{self.helm_bin.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
        chunks: list[str] = []
        for app in apps:
            cmd = [
                str(self.argocd_bin),
                "app",
                "diff",
                app,
                "--local",
                str(self.repo_path / "apps" / app),
                "--server",
                self.argocd_server,
                "--auth-token",
                self.argocd_token,
            ]
            if self.argocd_plaintext:
                cmd.append("--plaintext")
            result = self.runner.run(cmd, cwd=self.repo_path, env=env)
            # argocd exits 1 when differences exist, which is success here.
            if result.returncode not in (0, 1):
                # The token is a command-line argument, so argocd echoes it back
                # in some errors. Check details reach the model's context and the
                # PR body, so it has to come out here.
                detail = result.stderr.strip().replace(self.argocd_token, "***")
                return "", CheckResult(
                    name="diff",
                    passed=False,
                    detail=(detail[:400] or f"argocd diff failed for {app}"),
                )
            chunks.append(result.stdout)

        combined = "\n".join(chunks)
        if not combined.strip():
            return "", CheckResult(
                name="diff",
                passed=False,
                detail=(
                    "no_effective_change: the rendered manifests are identical to what is "
                    "already running, so this proposal changes nothing."
                ),
            )
        return combined, CheckResult("diff", True, "the change produces a real diff")

    def _kubectl_diff(self, rendered: str) -> tuple[str, CheckResult]:
        """Diff the *rendered* manifests against the cluster.

        Pointing kubectl at the repository directory instead reads every file in
        it — including the README and Chart.yaml — and fails on the first one
        that is not a manifest.
        """
        manifest_file = self.repo_path / ".kubemend-diff.yaml"
        manifest_file.write_text(rendered)
        try:
            result = self.runner.run(
                [str(self.kubectl_bin), "diff", "--server-side", "-f", str(manifest_file)],
                cwd=self.repo_path,
            )
        finally:
            manifest_file.unlink(missing_ok=True)
        # kubectl diff exits 1 when differences exist, which is success here.
        if result.returncode not in (0, 1):
            return "", CheckResult("diff", False, result.stderr.strip()[:400] or "diff failed")
        if not result.stdout.strip():
            return "", CheckResult(
                name="diff",
                passed=False,
                detail=(
                    "no_effective_change: the rendered manifests are identical to what is "
                    "already running, so this proposal changes nothing."
                ),
            )
        return result.stdout, CheckResult("diff", True, "the change produces a real diff")

    def _scope(self, resources: Sequence[Resource]) -> CheckResult:
        offenders = out_of_scope(resources, self.scope)
        if offenders:
            listed = ", ".join(f"{k}/{ns}/{n}" for k, ns, n in offenders)
            return CheckResult(
                name="scope",
                passed=False,
                detail=(
                    f"out of scope: {listed}. This run may only touch "
                    f"{self.scope.app} in namespace {self.scope.namespace}."
                ),
            )
        return CheckResult("scope", True, f"{len(resources)} resource(s), all in scope")

    def _quota(self, rendered: str) -> CheckResult:
        """Would the proposed replica count actually fit in the live namespace?

        Render, policy, diff, and scope all pass on a Deployment whose replica
        count the live ResourceQuota would refuse — the diff is real, in
        scope, and policy-clean, yet the resulting pods would sit Pending
        forever. Only checks `pods`, the one dimension the lab's own quota
        tracks; `requests.cpu`/`requests.memory` would need the same shape.
        A namespace can hold more than one workload against a shared quota, so
        this subtracts the resource's own current live contribution from what
        the quota already reports used, rather than assuming the quota is
        this app's alone.

        Skipped (passes) when no kube client is wired in, so existing fixtures
        that never touch quota semantics need no dummy client.
        """
        if self.kube is None:
            return CheckResult("quota", True, "no kube client wired in; quota check skipped")

        try:
            for doc in yaml.safe_load_all(rendered):
                if not isinstance(doc, dict) or doc.get("kind") not in (
                    "Deployment",
                    "StatefulSet",
                ):
                    continue
                metadata = doc.get("metadata") or {}
                namespace, name = metadata.get("namespace"), metadata.get("name")
                proposed = (doc.get("spec") or {}).get("replicas")
                if not namespace or not name or proposed is None:
                    continue

                failure = self._quota_headroom(namespace, name, int(proposed))
                if failure is not None:
                    return failure
        except ToolError as exc:
            return CheckResult("quota", False, f"could not check live quota headroom: {exc}")
        return CheckResult("quota", True, "proposed replica counts fit within live quota headroom")

    def _quota_headroom(self, namespace: str, name: str, proposed: int) -> CheckResult | None:
        assert self.kube is not None  # narrowed by the caller
        for quota in self.kube.list_resource("resourcequota", namespace):
            hard_pods = (quota.get("spec") or {}).get("hard", {}).get("pods")
            if hard_pods is None:
                continue
            used_pods = int((quota.get("status") or {}).get("used", {}).get("pods", 0))
            try:
                live = self.kube.get_resource("deployment", namespace, name)
                # status.replicas, not spec.replicas: spec is the *desired*
                # count, which can already be an unfulfillable number — that
                # is exactly the broken state being diagnosed. status.replicas
                # is how many pods this Deployment actually owns right now,
                # the quantity genuinely counted in the quota's current usage.
                # Reading spec here inverted the math: a live spec.replicas of
                # 6 against status quo pods=4 produced a *negative* "other
                # usage", making an over-quota proposal look like it fit.
                current_replicas = int((live.get("status") or {}).get("replicas", 0))
            except ToolError:
                # Not live yet (first-ever proposal for this app) — nothing of
                # its own to subtract from the quota's current usage.
                current_replicas = 0
            other_usage = used_pods - current_replicas
            projected = other_usage + proposed
            hard_pods_int = int(hard_pods)
            if projected > hard_pods_int:
                quota_name = (quota.get("metadata") or {}).get("name", "?")
                return CheckResult(
                    name="quota",
                    passed=False,
                    detail=(
                        f"{name}: {proposed} replicas would bring {namespace} to "
                        f"{projected} pods, exceeding quota {quota_name} "
                        f"(hard.pods={hard_pods_int}; other workloads in this namespace "
                        f"already use {other_usage})"
                    ),
                )
        return None


def _policy_evaluated(stdout: str) -> int:
    """Total rule results kyverno reported, however the run turned out."""
    total = 0
    for label in ("pass", "fail", "warn", "error", "skip"):
        match = re.search(rf"{label}:\s*(\d+)", stdout)
        if match:
            total += int(match.group(1))
    return total


def _policy_summary(stdout: str) -> str:
    for line in stdout.splitlines():
        if "pass" in line.lower() and "fail" in line.lower():
            return line.strip()
    return "all policies passed"


def _policy_failures(stdout: str, stderr: str) -> str:
    """Surface the failing rule verbatim.

    Convergence of the retry loop depends on this specificity: "disallow-latest-tag
    failed" tells the model what to change where a generic failure stalls it.
    """
    lines = [
        line.strip()
        for line in stdout.splitlines()
        if "fail" in line.lower() or "policy violation" in line.lower()
    ]
    if lines:
        return " | ".join(lines[:5])[:500]
    return (stderr.strip() or stdout.strip() or "policy check failed")[:400]
