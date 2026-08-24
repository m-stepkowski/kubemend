"""Lab operations for scenario runs (docs/knowledge/lab-and-evals.md).

Everything a scenario needs that is not the agent itself: resetting the gitops
repo to a known-good commit, committing the injected fault so Argo syncs it,
polling the cluster until the symptom has actually manifested (so a run does
not start diagnosing a state that has not happened yet), and re-rendering a
proposal branch for a checker's property assertions.

Talks to the live lab. Unit tests exercise the git operations against tmp_path
repos and the probe dispatch against fake kube/loki clients — nothing here
needs a real cluster to be tested; a real cluster is only involved when a sweep
actually runs (`task evals`).
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from git import GitCommandError, Repo

from evals.models import SymptomProbe
from kubemend.core.model import Scope
from kubemend.tools.observability.provider import LogQuery, LogResult


def loki_log_contains_query(scope: Scope, substring: str) -> LogQuery:
    """A server-side LogQL line filter, not a client-side scan.

    A pod can carry a noisy primary container (nginx's access log on every
    probe hit) alongside the sparse sidecar output a scenario actually cares
    about. Fetching the last N lines and scanning client-side lets the noisy
    stream crowd the sparse one out of the window entirely — the fix is
    asking Loki to filter before truncating, the same `|=` pattern documented
    for the agent's own search_logs tool.
    """
    escaped = substring.replace("\\", "\\\\").replace('"', '\\"')
    return LogQuery(
        query=f'{{namespace="{scope.namespace}", pod=~"{scope.app}-.*"}} |= "{escaped}"',
        start="-5m",
        end="now",
        limit=50,
    )


class LogContainsQueryBuilder(Protocol):
    def __call__(self, scope: Scope, substring: str) -> LogQuery: ...


class SymptomTimeout(Exception):
    """The injected fault never produced the state the scenario expects.

    Its own exception rather than a generic timeout: a scenario-authoring bug
    (wrong probe value, a break.patch that does not actually break anything)
    must not be indistinguishable from ordinary flakiness in a sweep report.
    """


class KubeQuery(Protocol):
    def list_resource(
        self, kind: str, namespace: str, selector: str | None = None
    ) -> list[dict[str, Any]]: ...

    def list_events(self, namespace: str, involved: str | None = None) -> list[dict[str, Any]]: ...


class LogSearch(Protocol):
    def search_logs(self, query: LogQuery) -> LogResult: ...


class Lab(Protocol):
    """What the runner and every checker need from a lab, structurally.

    `LabHandle` below is the real implementation; this Protocol is the seam
    that lets `run_sweep` and scenario checkers be tested against a fake
    without touching git or a cluster.
    """

    def snapshot(self) -> None: ...
    def reset(self) -> None: ...
    def apply_break(self, patch_path: Path, message: str) -> None: ...
    def wait_for_symptom(self, probe: SymptomProbe, scope: Scope) -> None: ...
    def render(self, app: str, namespace: str) -> str: ...
    def read_file(self, rel_path: str) -> str: ...


def _event_at(event: dict[str, Any]) -> datetime | None:
    """When an event last happened, across the two API shapes.

    Core v1 events carry `lastTimestamp`; the newer events.k8s.io shape uses
    `eventTime`. `firstTimestamp` is the last resort for an event that has
    only ever fired once. Returns None when none parse, and the caller treats
    an undateable event as stale — the safe direction, since the failure is a
    visible timeout rather than a run that starts against an unbroken cluster.
    """
    for key in ("lastTimestamp", "eventTime", "firstTimestamp"):
        raw = event.get(key)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


class LabHandle:
    def __init__(
        self,
        workspace: Path,
        base_branch: str,
        kube: KubeQuery,
        loki: LogSearch,
        *,
        remote: str = "origin",
        helm_bin: Path = Path(".lab/bin/helm"),
        kube_version: str = "1.31.2",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        log_query_builder: LogContainsQueryBuilder = loki_log_contains_query,
    ) -> None:
        self.workspace = workspace
        self.base_branch = base_branch
        self.kube = kube
        self.loki = loki
        self.remote = remote
        self.helm_bin = helm_bin
        self.kube_version = kube_version
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock
        self._log_query_builder = log_query_builder
        self._known_good_sha: str | None = None

    def _repo(self) -> Repo:
        return Repo(self.workspace)

    # -- gitops repo lifecycle --------------------------------------------

    def snapshot(self) -> None:
        """Record the current base-branch SHA as what reset() returns to.

        Call once before a sweep starts, on a clean checkout of the base
        branch. Every scenario's reset() comes back to this exact commit, so
        one scenario's leftovers never leak into the next.
        """
        repo = self._repo()
        repo.git.checkout(self.base_branch)
        repo.git.pull(self.remote, self.base_branch)
        self._known_good_sha = repo.head.commit.hexsha

    def reset(self) -> None:
        if self._known_good_sha is None:
            raise RuntimeError("snapshot() must run before reset()")
        repo = self._repo()
        repo.git.checkout(self.base_branch)
        repo.git.reset("--hard", self._known_good_sha)
        try:
            repo.git.push(self.remote, self.base_branch, "--force")
        except GitCommandError as exc:
            raise RuntimeError(f"could not reset {self.base_branch}: {exc}") from exc

    def apply_break(self, patch_path: Path, message: str) -> None:
        """Apply break.patch as a commit on the base branch and push it.

        A real commit, not a local-only working-tree edit: Argo only syncs
        what gitea has, and the symptom has to manifest in the real cluster
        before the agent is asked to diagnose it.
        """
        # git apply runs with cwd set to the workspace (this repo), not the
        # caller's — a relative patch path (the loader's default is relative
        # to the main kubemend checkout) resolves against the wrong directory
        # and fails with "No such file or directory".
        absolute_patch = Path(patch_path).resolve()
        repo = self._repo()
        try:
            repo.git.apply(str(absolute_patch))
        except GitCommandError as exc:
            raise RuntimeError(f"{absolute_patch} did not apply: {exc}") from exc
        repo.git.add(A=True)
        repo.index.commit(message)
        repo.git.push(self.remote, self.base_branch)

    # -- symptom probe ------------------------------------------------------

    def wait_for_symptom(self, probe: SymptomProbe, scope: Scope) -> None:
        # Anchored to when *this* wait began, so an `event_reason` probe cannot
        # be satisfied by an event a previous iteration left behind. Kubernetes
        # retains events for about an hour, which is far longer than a sweep's
        # iteration, and the stale match made quota-conflict start before Argo
        # had applied its break (see evals/reports/cheap-baseline/diagnosis.md).
        since = self._wall_clock()
        deadline = self._clock() + probe.timeout_s
        last_observed: str | bool = "no matching pod seen"
        while self._clock() < deadline:
            observed = self._probe_once(probe, scope, since)
            if observed is True:
                return
            last_observed = observed
            self._sleep(probe.poll_interval_s)
        raise SymptomTimeout(
            f"{probe.kind}={probe.value!r} never observed within {probe.timeout_s}s "
            f"for {scope.namespace}/{scope.app}; last observed: {last_observed}"
        )

    def _probe_once(self, probe: SymptomProbe, scope: Scope, since: datetime) -> bool | str:
        selector = f"app.kubernetes.io/name={scope.app}"
        if probe.kind == "pod_waiting_reason":
            return self._container_state_reason(scope, selector, "waiting", probe.value)
        if probe.kind == "pod_terminated_reason":
            return self._container_state_reason(scope, selector, "terminated", probe.value)
        if probe.kind == "pod_condition":
            return self._pod_condition(scope, selector, probe.condition_type, probe.value)
        if probe.kind == "event_reason":
            events = self.kube.list_events(scope.namespace)
            fresh = []
            for event in events:
                at = _event_at(event)
                if at is not None and at >= since:
                    fresh.append(event)
            if any(probe.value in str(e.get("reason", "")) for e in fresh):
                return True
            stale = len(events) - len(fresh)
            seen = [str(e.get("reason", "")) for e in fresh][-10:]
            if seen:
                return f"fresh event reasons: {seen}"
            # Naming the stale count matters: "no events" and "only events
            # older than this iteration" are very different diagnoses when a
            # probe times out.
            return f"no events since this iteration began ({stale} older ignored)"
        if probe.kind == "log_contains":
            return self._log_contains(scope, probe.value)
        raise ValueError(f"unknown symptom probe kind {probe.kind!r}")

    def _container_state_reason(
        self, scope: Scope, selector: str, state: str, want: str
    ) -> bool | str:
        """Check both `state` and `lastState` for the requested reason.

        A terminated container (OOMKilled, Error, ...) restarts automatically
        under the Deployment's default restart policy, and Kubernetes moves
        the evidence into lastState the moment it does — often faster than
        one poll interval. Checking only `state` misses every OOM that has
        already bounced back to Running by the time the probe looks.

        A container killed for memory *before* its own process starts is
        reported as `StartError` with an "OOM-killed" message, not as
        `OOMKilled` — observed live in this lab's containerd when a container
        overshoots its limit enough that even runc's own init step cannot
        fit. Both are genuinely memory exhaustion, so a probe looking for
        OOMKilled also accepts that message text.
        """
        pods = self.kube.list_resource("pod", scope.namespace, selector=selector)
        reasons = []
        for pod in pods:
            statuses = pod.get("status", {}).get("containerStatuses") or []
            for status in statuses:
                for slot in ("state", "lastState"):
                    info = status.get(slot, {}).get(state)
                    if info:
                        reason = str(info.get("reason", ""))
                        reasons.append(reason)
                        if reason == want:
                            return True
                        if want == "OOMKilled" and "OOM" in str(info.get("message", "")).upper():
                            return True
        return f"{state} reasons seen: {reasons}" if reasons else False

    def _pod_condition(
        self, scope: Scope, selector: str, condition_type: str, want_status: str
    ) -> bool | str:
        pods = self.kube.list_resource("pod", scope.namespace, selector=selector)
        observed = []
        for pod in pods:
            conditions = pod.get("status", {}).get("conditions") or []
            for condition in conditions:
                if condition.get("type") == condition_type:
                    status = str(condition.get("status", ""))
                    observed.append(status)
                    if status == want_status:
                        return True
        return f"{condition_type} statuses seen: {observed}" if observed else False

    def _log_contains(self, scope: Scope, substring: str) -> bool | str:
        result = self.loki.search_logs(self._log_query_builder(scope, substring))
        if any(stream.lines for stream in result.streams):
            return True
        return f"{result.total_lines} log line(s) seen, none matching" if result.streams else False

    # -- checker support ------------------------------------------------------

    def render(self, app: str, namespace: str) -> str:
        """Re-render the chart at whatever branch the workspace currently has
        checked out. The runner leaves that as the proposal branch after a run
        completes, the same state the agent's own validator rendered."""
        result = subprocess.run(
            [
                str(self.helm_bin),
                "template",
                app,
                str(self.workspace / "apps" / app),
                "--namespace",
                namespace,
                "--kube-version",
                self.kube_version,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"helm template failed for {app}: {result.stderr.strip()}")
        return result.stdout

    def read_file(self, rel_path: str) -> str:
        """Read a file from the workspace's current checkout, e.g. values.yaml."""
        target = self.workspace / rel_path
        if not target.is_file():
            raise FileNotFoundError(f"no file at {target}")
        return target.read_text()
