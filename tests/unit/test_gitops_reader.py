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
    ReaderRoute,
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


# -- no-match recovery (M12 §10c) -----------------------------------------


def test_an_unmatched_glob_returns_what_the_repo_actually_holds(repo: Path) -> None:
    """An empty list says the glob matched nothing, not that the *prefix* was
    wrong — so the next guess stays anchored to the same bad assumption. Two
    of three M12 acceptance runs died guessing apps/<namespace>/<app>/... and
    re-listing under that same wrong prefix until the loop detector fired.
    """
    result = GitOpsReader(repo).list("apps/shop-payments/checkout-api/**")

    assert result["paths"] == []
    assert "apps/shop-api/values.yaml" in result["repository_paths"]
    assert "no_match" in result


def test_a_matched_glob_carries_no_recovery_listing(repo: Path) -> None:
    """The listing is a recovery aid, not a routine payload — sending it on
    every successful call would swell context for no reason."""
    result = GitOpsReader(repo).list("apps/shop-api/**/*")

    assert "repository_paths" not in result
    assert "no_match" not in result


def test_the_recovery_listing_is_capped(repo: Path) -> None:
    from git import Repo

    from kubemend.tools.gitops.reader import MAX_LISTED_PATHS

    git_repo = Repo(repo)
    for i in range(MAX_LISTED_PATHS + 25):
        (repo / f"apps/shop-api/f{i:04d}.yaml").write_text("a: 1\n")
    git_repo.index.add([f"apps/shop-api/f{i:04d}.yaml" for i in range(MAX_LISTED_PATHS + 25)])
    git_repo.index.commit("many files")

    result = GitOpsReader(repo).list("nowhere/**")

    assert len(result["repository_paths"]) == MAX_LISTED_PATHS
    assert result["repository_paths_truncated"] is True


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
    readers = {"values": ReaderRoute(GitOpsReader(repo))}
    specs = [read_gitops_file_spec(readers), list_gitops_files_spec(readers)]

    assert [s.tier for s in specs] == ["read", "read"]

    before = sorted(p.name for p in repo.rglob("*"))
    specs[0].executor({"path": "apps/shop-api/values.yaml"})
    specs[1].executor({"pattern": "**/*"})
    assert sorted(p.name for p in repo.rglob("*")) == before


def test_executor_tolerates_a_missing_argument(repo: Path) -> None:
    """The model omits required fields; that must not raise into the loop."""
    readers = {"values": ReaderRoute(GitOpsReader(repo))}
    result = read_gitops_file_spec(readers).executor({})

    assert "error" in result


def test_repo_defaults_to_values_so_existing_calls_are_unaffected(repo: Path) -> None:
    readers = {"values": ReaderRoute(GitOpsReader(repo))}

    result = read_gitops_file_spec(readers).executor({"path": "apps/shop-api/values.yaml"})

    assert "content" in result


def test_chart_repo_reads_are_rooted_at_the_configured_chart_path(tmp_path: Path) -> None:
    """Split mode: the chart checkout's repo root isn't the chart root — a
    `chart_path` subdirectory prefix translates the model's chart-relative
    path into the checkout-relative one `git show` actually needs."""
    from git import Repo

    chart_checkout = tmp_path / "chart-checkout"
    (chart_checkout / "chart" / "templates").mkdir(parents=True)
    (chart_checkout / "chart" / "Chart.yaml").write_text("name: shop-api\n")
    git_repo = Repo.init(chart_checkout, initial_branch="main")
    git_repo.index.add(["chart/Chart.yaml"])
    git_repo.index.commit("seed")

    readers = {"chart": ReaderRoute(GitOpsReader(chart_checkout), prefix="chart")}

    result = read_gitops_file_spec(readers).executor({"path": "Chart.yaml", "repo": "chart"})

    assert result["path"] == "Chart.yaml", "echoes what the model asked for, not the prefixed path"
    assert "name: shop-api" in result["content"]


def test_chart_repo_listing_strips_the_prefix_back_off(tmp_path: Path) -> None:
    from git import Repo

    chart_checkout = tmp_path / "chart-checkout"
    (chart_checkout / "chart" / "templates").mkdir(parents=True)
    (chart_checkout / "chart" / "templates" / "deployment.yaml").write_text("kind: Deployment\n")
    git_repo = Repo.init(chart_checkout, initial_branch="main")
    git_repo.index.add(["chart/templates/deployment.yaml"])
    git_repo.index.commit("seed")

    readers = {"chart": ReaderRoute(GitOpsReader(chart_checkout), prefix="chart")}

    result = list_gitops_files_spec(readers).executor({"pattern": "**/*", "repo": "chart"})

    assert result["paths"] == ["templates/deployment.yaml"]


def test_chart_repo_no_match_listing_is_translated_to_the_models_path_space(
    tmp_path: Path,
) -> None:
    """The recovery listing is model-facing, so it gets the same prefix
    treatment reads do: stripped, and narrowed to the chart route — sibling
    paths outside chart_path are ones the model has no way to read back."""
    from git import Repo

    chart_checkout = tmp_path / "chart-checkout"
    (chart_checkout / "chart" / "templates").mkdir(parents=True)
    (chart_checkout / "chart" / "templates" / "deployment.yaml").write_text("kind: Deployment\n")
    (chart_checkout / "unrelated.md").write_text("not part of the chart\n")
    git_repo = Repo.init(chart_checkout, initial_branch="main")
    git_repo.index.add(["chart/templates/deployment.yaml", "unrelated.md"])
    git_repo.index.commit("seed")

    readers = {"chart": ReaderRoute(GitOpsReader(chart_checkout), prefix="chart")}

    result = list_gitops_files_spec(readers).executor(
        {"pattern": "nowhere/**", "repo": "chart"}
    )

    assert result["paths"] == []
    assert result["repository_paths"] == ["templates/deployment.yaml"]
    assert "unrelated.md" not in result["repository_paths"]


def test_chart_repo_in_single_repo_mode_returns_a_structured_client_error(repo: Path) -> None:
    """No "chart" key in the mapping at all — today's default wiring — is
    what single-repo mode looks like; the model must get a clear correction,
    not a KeyError into the loop."""
    readers = {"values": ReaderRoute(GitOpsReader(repo))}

    result = read_gitops_file_spec(readers).executor(
        {"path": "templates/deployment.yaml", "repo": "chart"}
    )

    assert result["error"]["type"] == "client_error"
    assert "single-repo setup" in result["error"]["message"]
