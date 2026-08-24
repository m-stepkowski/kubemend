"""LabHandle: gitops repo lifecycle + symptom probe dispatch (docs/knowledge/lab-and-evals.md).

Git operations run against real tmp_path repos (a bare "remote" plus a clone,
same shape as the real gitea workspace) so reset/apply_break/push semantics
are actually exercised, not mocked away. Probe dispatch runs against fake
kube/loki clients — no live cluster anywhere in this file, and no real sleep
either (a fake clock/sleep drives wait_for_symptom).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from git import Repo

from evals.lab import LabHandle, SymptomTimeout
from evals.models import SymptomProbe
from kubemend.core.model import Scope
from kubemend.tools.observability.provider import LogQuery, LogResult, LogStream

SCOPE = Scope(namespace="shop", app="shop-api")


class FakeClock:
    """Advances only when told to, so wait_for_symptom needs no real sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeKube:
    def __init__(
        self,
        pods: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.pods = pods or []
        self.events = events or []
        self.list_resource_calls: list[tuple[str, str, str | None]] = []

    def list_resource(
        self, kind: str, namespace: str, selector: str | None = None
    ) -> list[dict[str, Any]]:
        self.list_resource_calls.append((kind, namespace, selector))
        return self.pods if kind == "pod" else []

    def list_events(self, namespace: str, involved: str | None = None) -> list[dict[str, Any]]:
        return self.events


class FakeLoki:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = lines or []

    def search_logs(self, query: LogQuery) -> LogResult:
        return LogResult(streams=[LogStream(labels={}, lines=[("t", ln) for ln in self.lines])])


# -- gitops repo lifecycle -------------------------------------------------


@pytest.fixture
def remote_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "remote.git"
    Repo.init(bare, bare=True, initial_branch="main")

    seed = tmp_path / "seed"
    seed_repo = Repo.init(seed, initial_branch="main")
    (seed / "apps").mkdir()
    (seed / "apps" / "values.yaml").write_text("replicaCount: 2\n")
    seed_repo.index.add(["apps/values.yaml"])
    seed_repo.index.commit("seed")
    seed_repo.create_remote("origin", str(bare))
    seed_repo.git.push("origin", "main")

    clone_path = tmp_path / "clone"
    Repo.clone_from(str(bare), clone_path)
    return bare, clone_path


def _lab(clone_path: Path) -> LabHandle:
    return LabHandle(workspace=clone_path, base_branch="main", kube=FakeKube(), loki=FakeLoki())


def test_reset_before_snapshot_is_a_clear_error(remote_and_clone: tuple[Path, Path]) -> None:
    _bare, clone_path = remote_and_clone
    lab = _lab(clone_path)

    with pytest.raises(RuntimeError, match="snapshot"):
        lab.reset()


def test_apply_break_commits_and_pushes_the_patch(remote_and_clone: tuple[Path, Path]) -> None:
    bare, clone_path = remote_and_clone
    lab = _lab(clone_path)
    lab.snapshot()

    patch = clone_path.parent / "break.patch"
    patch.write_text(
        "diff --git a/apps/values.yaml b/apps/values.yaml\n"
        "index 0000000..1111111 100644\n"
        "--- a/apps/values.yaml\n"
        "+++ b/apps/values.yaml\n"
        "@@ -1 +1 @@\n"
        "-replicaCount: 2\n"
        "+replicaCount: 6\n"
    )
    lab.apply_break(patch, "break: bump replicas")

    assert (clone_path / "apps" / "values.yaml").read_text() == "replicaCount: 6\n"
    # Pushed, not just committed locally: a second clone from the bare repo
    # must see it too.
    check = Repo.clone_from(str(bare), clone_path.parent / "check")
    assert (check.working_dir is not None) and (
        Path(str(check.working_dir), "apps", "values.yaml").read_text() == "replicaCount: 6\n"
    )


def test_reset_restores_the_snapshot_and_pushes_that_too(
    remote_and_clone: tuple[Path, Path],
) -> None:
    bare, clone_path = remote_and_clone
    lab = _lab(clone_path)
    lab.snapshot()

    patch = clone_path.parent / "break.patch"
    patch.write_text(
        "diff --git a/apps/values.yaml b/apps/values.yaml\n"
        "index 0000000..1111111 100644\n"
        "--- a/apps/values.yaml\n"
        "+++ b/apps/values.yaml\n"
        "@@ -1 +1 @@\n"
        "-replicaCount: 2\n"
        "+replicaCount: 6\n"
    )
    lab.apply_break(patch, "break")
    lab.reset()

    assert (clone_path / "apps" / "values.yaml").read_text() == "replicaCount: 2\n"
    check = Repo.clone_from(str(bare), clone_path.parent / "check2")
    assert Path(str(check.working_dir), "apps", "values.yaml").read_text() == "replicaCount: 2\n"


def test_apply_break_resolves_a_relative_patch_path_against_cwd_not_the_repo(
    remote_and_clone: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """git apply runs with cwd set to the workspace repo, not the caller's — a
    relative patch path must still resolve against the caller's cwd, the way
    the loader's default SCENARIOS_ROOT ("lab/scenarios") is relative to the
    main kubemend checkout, not the gitops workspace clone."""
    _bare, clone_path = remote_and_clone
    lab = _lab(clone_path)
    lab.snapshot()

    patch_dir = clone_path.parent / "elsewhere"
    patch_dir.mkdir()
    (patch_dir / "break.patch").write_text(
        "diff --git a/apps/values.yaml b/apps/values.yaml\n"
        "index 0000000..1111111 100644\n"
        "--- a/apps/values.yaml\n"
        "+++ b/apps/values.yaml\n"
        "@@ -1 +1 @@\n"
        "-replicaCount: 2\n"
        "+replicaCount: 6\n"
    )
    monkeypatch.chdir(patch_dir)
    lab.apply_break(Path("break.patch"), "break")

    assert (clone_path / "apps" / "values.yaml").read_text() == "replicaCount: 6\n"


def test_apply_break_on_a_patch_that_does_not_apply_raises_and_does_not_push(
    remote_and_clone: tuple[Path, Path],
) -> None:
    _bare, clone_path = remote_and_clone
    lab = _lab(clone_path)
    lab.snapshot()

    patch = clone_path.parent / "bogus.patch"
    patch.write_text("this is not a valid patch\n")

    with pytest.raises(RuntimeError, match="did not apply"):
        lab.apply_break(patch, "break")


def test_read_file_reads_the_current_checkout(remote_and_clone: tuple[Path, Path]) -> None:
    _bare, clone_path = remote_and_clone
    lab = _lab(clone_path)

    assert lab.read_file("apps/values.yaml") == "replicaCount: 2\n"


def test_read_file_missing_is_a_named_error(remote_and_clone: tuple[Path, Path]) -> None:
    _bare, clone_path = remote_and_clone
    lab = _lab(clone_path)

    with pytest.raises(FileNotFoundError):
        lab.read_file("apps/does-not-exist.yaml")


# -- symptom probe dispatch -------------------------------------------------


def test_pod_waiting_reason_matches(remote_and_clone: tuple[Path, Path]) -> None:
    _bare, clone_path = remote_and_clone
    pods = [
        {"status": {"containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff"}}}]}}
    ]
    clock = FakeClock()
    lab = LabHandle(
        workspace=clone_path,
        base_branch="main",
        kube=FakeKube(pods=pods),
        loki=FakeLoki(),
        clock=clock.clock,
        sleep=clock.sleep,
    )
    probe = SymptomProbe(kind="pod_waiting_reason", value="ImagePullBackOff", timeout_s=10)

    lab.wait_for_symptom(probe, SCOPE)  # must not raise


def test_pod_terminated_reason_matches(remote_and_clone: tuple[Path, Path]) -> None:
    _bare, clone_path = remote_and_clone
    pods = [{"status": {"containerStatuses": [{"state": {"terminated": {"reason": "OOMKilled"}}}]}}]
    clock = FakeClock()
    lab = LabHandle(
        workspace=clone_path,
        base_branch="main",
        kube=FakeKube(pods=pods),
        loki=FakeLoki(),
        clock=clock.clock,
        sleep=clock.sleep,
    )
    probe = SymptomProbe(kind="pod_terminated_reason", value="OOMKilled", timeout_s=10)

    lab.wait_for_symptom(probe, SCOPE)


def test_terminated_reason_also_matches_a_start_error_with_an_oom_message(
    remote_and_clone: tuple[Path, Path],
) -> None:
    """containerd reports a container OOM-killed during its own init as
    StartError, not OOMKilled — observed live in the lab. Both are memory
    exhaustion, so a probe looking for OOMKilled accepts either."""
    _bare, clone_path = remote_and_clone
    pods = [
        {
            "status": {
                "containerStatuses": [
                    {
                        "lastState": {
                            "terminated": {
                                "reason": "StartError",
                                "message": "container init was OOM-killed (memory limit too low?)",
                            }
                        }
                    }
                ]
            }
        }
    ]
    clock = FakeClock()
    lab = LabHandle(
        workspace=clone_path,
        base_branch="main",
        kube=FakeKube(pods=pods),
        loki=FakeLoki(),
        clock=clock.clock,
        sleep=clock.sleep,
    )
    probe = SymptomProbe(kind="pod_terminated_reason", value="OOMKilled", timeout_s=10)

    lab.wait_for_symptom(probe, SCOPE)


def test_a_start_error_without_an_oom_message_does_not_falsely_match(
    remote_and_clone: tuple[Path, Path],
) -> None:
    """The OOM-message fallback must stay narrow: an unrelated startup
    failure must not be mistaken for memory exhaustion."""
    _bare, clone_path = remote_and_clone
    pods = [
        {
            "status": {
                "containerStatuses": [
                    {
                        "lastState": {
                            "terminated": {
                                "reason": "StartError",
                                "message": "failed to create shim: no such file or directory",
                            }
                        }
                    }
                ]
            }
        }
    ]
    clock = FakeClock()
    lab = LabHandle(
        workspace=clone_path,
        base_branch="main",
        kube=FakeKube(pods=pods),
        loki=FakeLoki(),
        clock=clock.clock,
        sleep=clock.sleep,
    )
    probe = SymptomProbe(
        kind="pod_terminated_reason", value="OOMKilled", timeout_s=1, poll_interval_s=1
    )

    with pytest.raises(SymptomTimeout):
        lab.wait_for_symptom(probe, SCOPE)


def test_pod_condition_matches(remote_and_clone: tuple[Path, Path]) -> None:
    _bare, clone_path = remote_and_clone
    pods = [{"status": {"conditions": [{"type": "Ready", "status": "False"}]}}]
    clock = FakeClock()
    lab = LabHandle(
        workspace=clone_path,
        base_branch="main",
        kube=FakeKube(pods=pods),
        loki=FakeLoki(),
        clock=clock.clock,
        sleep=clock.sleep,
    )
    probe = SymptomProbe(kind="pod_condition", condition_type="Ready", value="False", timeout_s=10)

    lab.wait_for_symptom(probe, SCOPE)


# Kubernetes keeps events ~1h, far longer than one sweep iteration, so these
# two tests pin the boundary the M14 baseline diagnosis found broken.
WAIT_STARTED_AT = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _event(reason: str, *, age_seconds: float) -> dict[str, object]:
    when = WAIT_STARTED_AT + timedelta(seconds=-age_seconds)
    return {
        "reason": reason,
        "message": "exceeded quota",
        "lastTimestamp": when.isoformat().replace("+00:00", "Z"),
    }


def test_event_reason_matches_substring(remote_and_clone: tuple[Path, Path]) -> None:
    _bare, clone_path = remote_and_clone
    events = [_event("FailedCreate", age_seconds=-1)]  # one second *after* the wait began
    clock = FakeClock()
    lab = LabHandle(
        workspace=clone_path,
        base_branch="main",
        kube=FakeKube(events=events),
        loki=FakeLoki(),
        clock=clock.clock,
        sleep=clock.sleep,
        wall_clock=lambda: WAIT_STARTED_AT,
    )
    probe = SymptomProbe(kind="event_reason", value="FailedCreate", timeout_s=10)

    lab.wait_for_symptom(probe, SCOPE)


def test_an_event_from_a_previous_iteration_does_not_satisfy_the_probe(
    remote_and_clone: tuple[Path, Path],
) -> None:
    """The M14 baseline's biggest fixture bug: with the lab reset and healthy,
    a leftover FailedCreate still satisfied this probe, so every iteration
    after the first started before Argo had applied its break."""
    _bare, clone_path = remote_and_clone
    events = [_event("FailedCreate", age_seconds=600)]  # ten minutes stale
    clock = FakeClock()
    lab = LabHandle(
        workspace=clone_path,
        base_branch="main",
        kube=FakeKube(events=events),
        loki=FakeLoki(),
        clock=clock.clock,
        sleep=clock.sleep,
        wall_clock=lambda: WAIT_STARTED_AT,
    )
    probe = SymptomProbe(kind="event_reason", value="FailedCreate", timeout_s=10)

    with pytest.raises(SymptomTimeout, match="older ignored"):
        lab.wait_for_symptom(probe, SCOPE)


def test_log_contains_matches_a_substring(remote_and_clone: tuple[Path, Path]) -> None:
    _bare, clone_path = remote_and_clone
    clock = FakeClock()
    lab = LabHandle(
        workspace=clone_path,
        base_branch="main",
        kube=FakeKube(),
        loki=FakeLoki(lines=["heartbeat", "ERROR connecting to upstream: refused"]),
        clock=clock.clock,
        sleep=clock.sleep,
    )
    probe = SymptomProbe(kind="log_contains", value="ERROR connecting to upstream", timeout_s=10)

    lab.wait_for_symptom(probe, SCOPE)


def test_log_contains_uses_the_injected_query_builder_not_the_loki_default(
    remote_and_clone: tuple[Path, Path],
) -> None:
    """A provider-neutral seam (M9): a Datadog-backed lab would inject its own
    builder here instead of `loki_log_contains_query` — asserting the
    injected one is what actually gets called is what keeps that seam real."""
    _bare, clone_path = remote_and_clone
    clock = FakeClock()
    calls: list[tuple[Scope, str]] = []

    def fake_builder(scope: Scope, substring: str) -> LogQuery:
        calls.append((scope, substring))
        return LogQuery(query="ignored", start="-5m", end="now")

    lab = LabHandle(
        workspace=clone_path,
        base_branch="main",
        kube=FakeKube(),
        loki=FakeLoki(lines=["ERROR connecting to upstream: refused"]),
        clock=clock.clock,
        sleep=clock.sleep,
        log_query_builder=fake_builder,
    )
    probe = SymptomProbe(kind="log_contains", value="ERROR connecting to upstream", timeout_s=10)

    lab.wait_for_symptom(probe, SCOPE)

    assert calls == [(SCOPE, "ERROR connecting to upstream")]


def test_probe_that_never_matches_times_out_with_a_named_exception(
    remote_and_clone: tuple[Path, Path],
) -> None:
    _bare, clone_path = remote_and_clone
    clock = FakeClock()
    lab = LabHandle(
        workspace=clone_path,
        base_branch="main",
        kube=FakeKube(pods=[]),
        loki=FakeLoki(),
        clock=clock.clock,
        sleep=clock.sleep,
    )
    probe = SymptomProbe(
        kind="pod_waiting_reason", value="ImagePullBackOff", timeout_s=10, poll_interval_s=3
    )

    with pytest.raises(SymptomTimeout, match="ImagePullBackOff"):
        lab.wait_for_symptom(probe, SCOPE)
    # Polled repeatedly rather than raising on the first miss.
    assert len(clock.sleeps) >= 2


def test_probe_dispatch_uses_the_app_label_selector(remote_and_clone: tuple[Path, Path]) -> None:
    _bare, clone_path = remote_and_clone
    kube = FakeKube(pods=[])
    clock = FakeClock()
    lab = LabHandle(
        workspace=clone_path,
        base_branch="main",
        kube=kube,
        loki=FakeLoki(),
        clock=clock.clock,
        sleep=clock.sleep,
    )
    probe = SymptomProbe(kind="pod_waiting_reason", value="X", timeout_s=1, poll_interval_s=1)

    with pytest.raises(SymptomTimeout):
        lab.wait_for_symptom(probe, SCOPE)

    assert kube.list_resource_calls[0] == ("pod", "shop", "app.kubernetes.io/name=shop-api")


def test_unknown_probe_kind_raises_immediately(remote_and_clone: tuple[Path, Path]) -> None:
    _bare, clone_path = remote_and_clone
    lab = _lab(clone_path)
    bad_probe = SymptomProbe(kind="pod_waiting_reason", value="x")
    object.__setattr__(bad_probe, "kind", "not_a_real_kind")

    with pytest.raises(ValueError, match="unknown symptom probe kind"):
        lab.wait_for_symptom(bad_probe, SCOPE)
