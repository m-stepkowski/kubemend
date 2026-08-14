"""Validation pipeline (ARCHITECTURE.md §5).

Four stages against the active branch's working tree: helm template, Kyverno
policy check against the project's own pack, live diff (argocd app diff, falling
back to kubectl diff --server-side), and the harness-owned scope check. An empty
diff fails as `no_effective_change` — it catches the model "fixing" a value by
rewriting it to itself.

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
from typing import Protocol

from kubemend.core.model import CheckResult, DiffSummary, Scope, Verdict

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
    runner: CommandRunner = field(default_factory=SubprocessRunner)

    @property
    def _uses_argocd(self) -> bool:
        return bool(self.argocd_bin and self.argocd_token)

    def validate(self, apps: Sequence[str]) -> Verdict:
        """Run the four stages, stopping at the first failure.

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
        return Verdict(
            passed=scope_check.passed,
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
