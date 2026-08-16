"""Container image guards (M8a).

Two layers: a fast, always-on check that the pinned tool versions in
`Dockerfile` haven't silently drifted from `Taskfile.yaml`'s `vars:` block
(the two places these versions live — see the Dockerfile's own comment on
this), and a `docker`-marked suite that builds the real image and runs it.

Building the second suite caught two real bugs a code review would not
have: `kubemend.cli` unconditionally importing the dev-only `evals` package
(deliberately excluded from what ships), and the slim base image missing
the `git` binary GitPython checks for at import time. Both are fixed in the
Dockerfile/kubemend.cli; this file is what keeps them fixed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_TAG = "kubemend:test-image"

# Taskfile.yaml var name -> Dockerfile ARG name. Both pin the same four
# tools; this mapping is what test_dockerfile_versions_match_taskfile
# cross-checks so a bump in one and not the other fails loudly instead of
# silently reintroducing the "PATH helm a version ahead" bug class.
_VERSION_VARS = {
    "HELM_VERSION": "HELM_VERSION",
    "KYVERNO_VERSION": "KYVERNO_VERSION",
    "KUBECTL_VERSION": "KUBECTL_VERSION",
    "ARGOCD_VERSION": "ARGOCD_VERSION",
}


def _taskfile_versions() -> dict[str, str]:
    text = (REPO_ROOT / "Taskfile.yaml").read_text()
    versions = {}
    for name in _VERSION_VARS:
        match = re.search(rf"^\s*{name}:\s*(\S+)", text, re.MULTILINE)
        assert match, f"{name} not found in Taskfile.yaml's vars: block"
        versions[name] = match.group(1)
    return versions


def _dockerfile_versions() -> dict[str, str]:
    text = (REPO_ROOT / "Dockerfile").read_text()
    versions = {}
    for taskfile_name, arg_name in _VERSION_VARS.items():
        match = re.search(rf"^ARG {arg_name}=(\S+)", text, re.MULTILINE)
        assert match, f"ARG {arg_name} not found in Dockerfile"
        versions[taskfile_name] = match.group(1)
    return versions


def test_dockerfile_versions_match_taskfile() -> None:
    """Fast, no Docker required — the drift check itself."""
    assert _dockerfile_versions() == _taskfile_versions()


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


@pytest.fixture(scope="module")
def built_image() -> str:
    if not _docker_available():
        pytest.skip("no Docker daemon available")
    result = subprocess.run(
        ["docker", "buildx", "build", "-t", IMAGE_TAG, "--load", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"docker build failed:\n{result.stderr[-4000:]}")
    return IMAGE_TAG


def _run_in_image(image: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "/bin/sh", image, "-c", " ".join(args)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.docker
def test_console_script_resolves_without_the_dev_only_evals_package(built_image: str) -> None:
    """The real regression this file exists for: `kubemend.cli` used to
    import `evals.runner.evals_app` unconditionally, but `evals/` is
    deliberately not part of the wheel — every invocation of a real install
    failed before this was fixed."""
    result = _run_in_image(built_image, "kubemend --help")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "evals" not in result.stdout, "evals/ isn't shipped; the subcommand must not appear"
    assert "run" in result.stdout
    assert "trace" in result.stdout


@pytest.mark.docker
def test_git_binary_is_present(built_image: str) -> None:
    """GitPython checks for a real `git` executable at import time, not
    lazily — a slim base with no git binary breaks every invocation, not
    just ones that touch a repo."""
    result = _run_in_image(built_image, "git --version")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "git version" in result.stdout


@pytest.mark.docker
def test_pricing_table_is_present_at_its_baked_in_default(built_image: str) -> None:
    """config/pricing.yaml lives at the repo top level, like evals/ — outside
    the kubemend/ package hatchling ships. Missing it doesn't crash a run
    (load_pricing() falls back to FALLBACK_PRICE for every model), it just
    silently reports wrong costs, so there's no exit-code signal to catch
    this if it regresses — this test has to check the file directly."""
    result = _run_in_image(built_image, "test -s $KUBEMEND_MODEL__PRICING_TABLE")

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.docker
def test_helm_chart_is_baked_in_and_renders(built_image: str) -> None:
    """The operator (M8b) shells out to `helm template` against this chart to
    create incident Jobs — same class of gap as pricing.yaml/policies/ above
    if it's ever missing from the image, except here the failure mode is a
    hard error (helm can't find the chart) rather than a silent one."""
    result = _run_in_image(
        built_image,
        "$KUBEMEND_BIN_DIR/helm template $KUBEMEND_OPERATOR__CHART_DIR "
        "-s templates/job.yaml --set job.enabled=true --set job.namespace=x "
        "--set job.app=y --set 'job.task=z' > /dev/null",
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.docker
@pytest.mark.parametrize("binary", ["helm", "kubectl", "kyverno", "argocd"])
def test_pinned_binary_is_present_at_the_dockerfile_pinned_version(
    built_image: str, binary: str
) -> None:
    """Checks the built image against what the Dockerfile itself declares —
    build-process correctness (the right URL, the right archive layout),
    distinct from test_dockerfile_versions_match_taskfile's cross-file
    drift check above."""
    dockerfile_versions = _dockerfile_versions()
    version_key = {
        "helm": "HELM_VERSION",
        "kubectl": "KUBECTL_VERSION",
        "kyverno": "KYVERNO_VERSION",
        "argocd": "ARGOCD_VERSION",
    }[binary]
    # kyverno's own `version` output omits the leading "v" Taskfile pins with.
    expected = dockerfile_versions[version_key].lstrip("v")

    bin_dir = "/usr/local/lib/kubemend-tools"
    result = _run_in_image(built_image, f"{bin_dir}/{binary} version 2>&1 || true")

    assert expected in result.stdout, f"{binary}: expected {expected!r} in {result.stdout!r}"


@pytest.mark.docker
def test_runs_as_a_non_root_user(built_image: str) -> None:
    result = _run_in_image(built_image, "whoami")

    assert result.stdout.strip() == "kubemend"
