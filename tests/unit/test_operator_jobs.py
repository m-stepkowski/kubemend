"""Job creation via helm+kubectl (docs/knowledge/operator-design.md)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from kubemend.core.model import Scope, Task
from kubemend.operator.jobs import JobCreated, JobCreationFailed, create_job

TASK = Task(statement="ShopApiCrashLooping: crash-looping", scope=Scope("shop", "shop-api"))
JOB_YAML = "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: kubemend-run-123\n"


class _Recorder:
    """Fakes subprocess.run, recording every call and returning queued results."""

    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": argv, **kwargs})
        return self._results.pop(0)


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def _create_job(recorder: _Recorder) -> JobCreated | JobCreationFailed:
    return create_job(
        TASK,
        chart_dir=Path("/chart"),
        values_file=Path("/values.yaml"),
        helm_bin=Path("/bin/helm"),
        kubectl_bin=Path("/bin/kubectl"),
        namespace="kubemend-system",
        release_name="kubemend",
    )


def test_success_returns_the_job_name(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder([_ok(JOB_YAML), _ok("job.batch/kubemend-run-123 created")])
    monkeypatch.setattr(subprocess, "run", recorder)

    result = _create_job(recorder)

    assert result == JobCreated(name="kubemend-run-123")
    assert len(recorder.calls) == 2


def test_helm_render_argv_is_a_list_never_a_shell_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alert text can contain shell metacharacters — subprocess.run must
    receive a list argv (no shell=True) so nothing is ever interpreted."""
    recorder = _Recorder([_ok(JOB_YAML), _ok()])
    monkeypatch.setattr(subprocess, "run", recorder)

    _create_job(recorder)

    helm_call = recorder.calls[0]
    assert helm_call["argv"][0] == "/bin/helm"
    assert "template" in helm_call["argv"]
    assert "-s" in helm_call["argv"]
    assert "templates/job.yaml" in helm_call["argv"]
    assert helm_call.get("shell", False) is False
    assert helm_call["input"] is not None


def test_helm_template_is_called_with_the_real_release_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`helm template <chart>` without a release name silently defaults
    .Release.Name to the literal "release-name" — every spawned Job would
    then reference a ConfigMap that doesn't exist (kubemend.fullname
    resolves off .Release.Name). Found by running this against a real
    cluster, not by review."""
    recorder = _Recorder([_ok(JOB_YAML), _ok()])
    monkeypatch.setattr(subprocess, "run", recorder)

    _create_job(recorder)

    helm_call = recorder.calls[0]
    template_index = helm_call["argv"].index("template")
    assert helm_call["argv"][template_index + 1] == "kubemend"
    assert "release-name" not in helm_call["argv"]


def test_dynamic_fields_go_through_stdin_not_set_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """--set is Helm's own mini-DSL where commas/equals/backslashes are
    meaningful — alert text must never be interpolated into an argv token."""
    task = Task(statement="alert with a comma, and an = sign", scope=Scope("shop", "shop-api"))
    recorder = _Recorder([_ok(JOB_YAML), _ok()])
    monkeypatch.setattr(subprocess, "run", recorder)

    create_job(
        task,
        chart_dir=Path("/chart"),
        values_file=Path("/values.yaml"),
        helm_bin=Path("/bin/helm"),
        kubectl_bin=Path("/bin/kubectl"),
        namespace="kubemend-system",
        release_name="kubemend",
    )

    helm_call = recorder.calls[0]
    assert not any(arg == "--set" for arg in helm_call["argv"])
    assert task.statement in helm_call["input"]


def test_kubectl_receives_helms_rendered_output_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder([_ok(JOB_YAML), _ok()])
    monkeypatch.setattr(subprocess, "run", recorder)

    _create_job(recorder)

    kubectl_call = recorder.calls[1]
    assert kubectl_call["argv"][0] == "/bin/kubectl"
    assert kubectl_call["input"] == JOB_YAML


def test_helm_template_failure_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder([_fail("boom")])
    monkeypatch.setattr(subprocess, "run", recorder)

    result = _create_job(recorder)

    assert isinstance(result, JobCreationFailed)
    assert "boom" in result.detail
    assert len(recorder.calls) == 1, "kubectl must not run when the render already failed"


def test_malformed_render_output_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder([_ok("not: [valid, yaml: for a job")])
    monkeypatch.setattr(subprocess, "run", recorder)

    result = _create_job(recorder)

    assert isinstance(result, JobCreationFailed)


def test_kubectl_create_failure_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder([_ok(JOB_YAML), _fail("forbidden")])
    monkeypatch.setattr(subprocess, "run", recorder)

    result = _create_job(recorder)

    assert isinstance(result, JobCreationFailed)
    assert "forbidden" in result.detail
