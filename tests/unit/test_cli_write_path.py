"""`build_write_path`'s split-mode wiring (M11 Phase 4).

`build_kube_client` is monkeypatched everywhere here: it eagerly loads a real
kubeconfig, which this suite has no business needing just to prove
`chart_route` threads through correctly to `Validator`/`GiteaBackend`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from git import Repo

import kubemend.cli as cli_module
from kubemend.config import GitOpsConfig, RunConfig
from kubemend.core.model import Scope
from kubemend.tools.gitops.gitea_backend import GiteaBackend
from kubemend.tools.gitops.local_backend import LocalGitBackend
from kubemend.tools.gitops.routing import ChartRoute

SCOPE = Scope(namespace="shop", app="shop-api")


@pytest.fixture(autouse=True)
def _no_real_kubeconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "build_kube_client", lambda _cfg: None)


def _route(tmp_path: Path) -> ChartRoute:
    return ChartRoute(
        checkout_root=tmp_path / "chart-checkout",
        chart_path="chart",
        base_branch="main",
        url="https://git.corp/shop-api.git",
    )


def _values_repo(tmp_path: Path) -> Path:
    """`LocalGitBackend`/`GiteaBackend` both construct a real `git.Repo` at
    init time; a bare directory raises `ClientError` before this test gets
    anywhere near what it's actually checking."""
    repo_path = tmp_path / "gitops-workspace"
    Repo.init(repo_path, initial_branch="main")
    return repo_path


def test_single_repo_mode_passes_no_chart_dirs(tmp_path: Path) -> None:
    cfg = RunConfig(gitops=GitOpsConfig(backend="local", repo_path=_values_repo(tmp_path)))

    _, gate = cli_module.build_write_path(cfg, SCOPE, "run1", tmp_path, chart_route=None)

    assert gate.validator.chart_dirs is None
    assert isinstance(gate.proposer.backend, LocalGitBackend)


def test_split_mode_with_local_backend_fails_fast(tmp_path: Path) -> None:
    (tmp_path / "gitops-workspace").mkdir()
    cfg = RunConfig(gitops=GitOpsConfig(backend="local", repo_path=tmp_path / "gitops-workspace"))

    with pytest.raises(typer.BadParameter, match="split mode's diff needs a forge backend"):
        cli_module.build_write_path(cfg, SCOPE, "run1", tmp_path, chart_route=_route(tmp_path))


def test_split_mode_with_gitea_backend_threads_chart_dirs_and_push_on_write(
    tmp_path: Path,
) -> None:
    repo_path = _values_repo(tmp_path)
    token_file = tmp_path / "gitea-token"
    token_file.write_text("secret\n")
    cfg = RunConfig(
        gitops=GitOpsConfig(backend="gitea", repo_path=repo_path, gitea_token_file=token_file)
    )
    route = _route(tmp_path)

    _, gate = cli_module.build_write_path(cfg, SCOPE, "run1", tmp_path, chart_route=route)

    assert gate.validator.chart_dirs == {"shop-api": route.chart_dir}
    assert gate.validator.run_id == "run1"
    assert isinstance(gate.proposer.backend, GiteaBackend)
    assert gate.proposer.backend.push_on_write is True


def test_single_repo_mode_gitea_backend_never_pushes_on_write(tmp_path: Path) -> None:
    """Regression guard: single-repo mode's timing must stay exactly as
    before M11 — push only after a verified proposal, never mid-loop."""
    repo_path = _values_repo(tmp_path)
    token_file = tmp_path / "gitea-token"
    token_file.write_text("secret\n")
    cfg = RunConfig(
        gitops=GitOpsConfig(backend="gitea", repo_path=repo_path, gitea_token_file=token_file)
    )

    _, gate = cli_module.build_write_path(cfg, SCOPE, "run1", tmp_path, chart_route=None)

    assert isinstance(gate.proposer.backend, GiteaBackend)
    assert gate.proposer.backend.push_on_write is False
