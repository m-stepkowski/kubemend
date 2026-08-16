"""Job creation via helm+kubectl (docs/knowledge/operator-design.md).

Shells out to the pinned helm/kubectl binaries — the same pattern
`kubemend/tools/gitops/validator.py` already uses for helm/kyverno/argocd/
kubectl — rather than building the Job manifest as a hand-rolled Python
dict. This reuses the exact Job shape and escape hatches
(extraInitContainers, env/envFrom) the manual `helm template ... | kubectl
create` path (charts/kubemend's own NOTES.txt) already has, instead of
duplicating them and risking drift between two representations.

The three per-alert dynamic fields (namespace, app, task) are passed as a
YAML values file on helm's stdin rather than `--set` flags: alert text can
contain commas, equals signs, or backslashes, all of which are meaningful in
Helm's `--set` mini-syntax. A YAML document sidesteps that escaping problem
entirely — `task.statement` becomes a plain, safely-quoted YAML string.

`release_name` must be the operator's own actual Helm release name, not a
placeholder: `templates/job.yaml` references the pre-existing `kubemend.yaml`
ConfigMap by `{{ include "kubemend.fullname" . }}-config`, which resolves to
`.Release.Name`-config. `helm template <chart>` without a release name
silently defaults `.Release.Name` to the literal string `"release-name"`,
producing a Job that mounts a ConfigMap that doesn't exist — this is not a
hypothetical, it's what happened the first time this was tested end to end.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from kubemend.core.model import Task


@dataclass(frozen=True)
class JobCreated:
    name: str


@dataclass(frozen=True)
class JobCreationFailed:
    detail: str


def create_job(
    task: Task,
    *,
    chart_dir: Path,
    values_file: Path,
    helm_bin: Path,
    kubectl_bin: Path,
    namespace: str,
    release_name: str,
) -> JobCreated | JobCreationFailed:
    """Renders the chart's Job template and applies it.

    Never raises — every failure mode (bad template, cluster rejection)
    returns `JobCreationFailed` so the caller can log a structured decision
    line, the same "errors are information" spirit as I2, applied here even
    though this sits outside `core/loop.py`.
    """
    dynamic_values = yaml.safe_dump(
        {
            "job": {
                "enabled": True,
                "namespace": task.scope.namespace,
                "app": task.scope.app,
                "task": task.statement,
            }
        }
    )

    render = subprocess.run(
        [
            str(helm_bin),
            "template",
            release_name,
            str(chart_dir),
            "-s",
            "templates/job.yaml",
            "-f",
            str(values_file),
            "-f",
            "-",
            "--namespace",
            namespace,
        ],
        input=dynamic_values,
        capture_output=True,
        text=True,
        check=False,
    )
    if render.returncode != 0:
        return JobCreationFailed(f"helm template failed: {render.stderr.strip()[:500]}")

    try:
        manifest = yaml.safe_load(render.stdout)
        job_name = manifest["metadata"]["name"]
    except (yaml.YAMLError, KeyError, TypeError) as exc:
        return JobCreationFailed(f"rendered manifest is not a valid Job: {exc}")

    apply = subprocess.run(
        [str(kubectl_bin), "create", "-n", namespace, "-f", "-"],
        input=render.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    if apply.returncode != 0:
        return JobCreationFailed(f"kubectl create failed: {apply.stderr.strip()[:500]}")

    return JobCreated(name=job_name)
