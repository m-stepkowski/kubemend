"""Read access to the GitOps repo (ARCHITECTURE.md §4.1).

The confinement tests matter most: these tools take a model-supplied path, so
they are the one read surface where a crafted string could reach outside the
repository. Everything here must fail closed and return an error rather than
raise (invariant I2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kubemend.tools.gitops.reader import (
    GitOpsReader,
    list_gitops_files_spec,
    read_gitops_file_spec,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    from git import Repo

    git_repo = Repo.init(tmp_path, initial_branch="main")
    (tmp_path / "apps/shop-api/templates").mkdir(parents=True)
    (tmp_path / "apps/shop-api/values.yaml").write_text("replicaCount: 2\nservice:\n  port: 8080\n")
    (tmp_path / "apps/shop-api/templates/service.yaml").write_text(
        "port: {{ .Values.service.port }}\n"
    )
    git_repo.index.add(["apps/shop-api/values.yaml", "apps/shop-api/templates/service.yaml"])
    git_repo.index.commit("seed")
    return tmp_path


def test_reads_the_file_the_model_must_rewrite(repo: Path) -> None:
    result = GitOpsReader(repo).read("apps/shop-api/values.yaml")

    assert "service:" in result["content"], "the field the first lab run dropped"
    assert result["truncated"] is False


def test_templates_are_readable_so_required_values_are_discoverable(repo: Path) -> None:
    """Reads are deliberately wider than writable_globs."""
    result = GitOpsReader(repo).read("apps/shop-api/templates/service.yaml")

    assert ".Values.service.port" in result["content"]


@pytest.mark.parametrize(
    "path",
    [
        "../../../etc/passwd",
        "apps/../../outside.yaml",
        "/etc/passwd",
    ],
)
def test_traversal_is_refused_as_an_error_not_an_exception(repo: Path, path: str) -> None:
    result = GitOpsReader(repo).read(path)

    assert result["error"]["type"] == "path_not_readable"


def test_git_internals_are_never_readable(repo: Path) -> None:
    """.git holds the push credential this backend authenticates with."""
    result = GitOpsReader(repo).read(".git/config")

    assert result["error"]["type"] == "path_not_readable"


def test_reads_the_base_branch_not_the_proposal_the_model_just_wrote(repo: Path) -> None:
    """The bug that made the second lab run diagnose the chart instead of the tag.

    The proposer leaves the checkout on the proposal branch. If reads came from
    the working tree, the model would read back its own truncated values.yaml
    and treat it as ground truth, compounding its own error every iteration.
    """
    from git import Repo

    git_repo = Repo(repo)
    git_repo.git.checkout("-b", "kubemend/run1")
    (repo / "apps/shop-api/values.yaml").write_text("replicaCount: 2\n")  # service.port dropped
    git_repo.index.add(["apps/shop-api/values.yaml"])
    git_repo.index.commit("a bad proposal")

    result = GitOpsReader(repo).read("apps/shop-api/values.yaml")

    assert "port: 8080" in result["content"], "the base branch is still the source of truth"


def test_missing_file_is_a_named_error(repo: Path) -> None:
    result = GitOpsReader(repo).read("apps/shop-api/values-prod.yaml")

    assert result["error"]["type"] == "not_found"


def test_listing_excludes_git_and_is_repo_relative(repo: Path) -> None:
    result = GitOpsReader(repo).list("**/*")

    assert "apps/shop-api/values.yaml" in result["paths"]
    assert not any(p.startswith(".git") for p in result["paths"])
    assert not any(p.startswith("/") for p in result["paths"])


def test_listing_narrows_to_a_chart(repo: Path) -> None:
    result = GitOpsReader(repo).list("apps/shop-api/**/*")

    assert "apps/shop-api/templates/service.yaml" in result["paths"]


def test_oversized_file_is_truncated_rather_than_flooding_context(repo: Path) -> None:
    from git import Repo

    (repo / "apps/shop-api/big.yaml").write_text("x: " + "y" * 100_000 + "\n")
    git_repo = Repo(repo)
    git_repo.index.add(["apps/shop-api/big.yaml"])
    git_repo.index.commit("big")

    result = GitOpsReader(repo).read("apps/shop-api/big.yaml")

    assert result["truncated"] is True
    assert len(result["content"].encode()) <= 64_000


def test_tool_specs_are_read_tier_and_side_effect_free(repo: Path) -> None:
    """A write-capable read tool would breach the single-write-path invariant."""
    reader = GitOpsReader(repo)
    specs = [read_gitops_file_spec(reader), list_gitops_files_spec(reader)]

    assert [s.tier for s in specs] == ["read", "read"]

    before = sorted(p.name for p in repo.rglob("*"))
    specs[0].executor({"path": "apps/shop-api/values.yaml"})
    specs[1].executor({"pattern": "**/*"})
    assert sorted(p.name for p in repo.rglob("*")) == before


def test_executor_tolerates_a_missing_argument(repo: Path) -> None:
    """The model omits required fields; that must not raise into the loop."""
    result = read_gitops_file_spec(GitOpsReader(repo)).executor({})

    assert "error" in result
