"""GiteaBackend's push timing (M11 finding, not in the original design doc).

Split mode's Argo CD multi-source diff reads a pushed ref (`--revisions`), not
the local working tree, unlike single-repo's `--local` — so the run branch
must exist on the remote *before* verification runs, not only after a
verified proposal (`open_draft_pr`, today's only push point). `push_on_write`
is the fix; these tests prove both settings behave as claimed, against real
git repos (a real "remote" bare repo, not a mock) so a push either really
happened or really didn't.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from git import Actor, Repo

from kubemend.tools.gitops.gitea_backend import GiteaBackend

AUTHOR = Actor("kubemend", "kubemend@localhost")


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    return Repo.init(tmp_path / "remote.git", bare=True).working_dir  # type: ignore[return-value]


@pytest.fixture
def local_repo(tmp_path: Path, remote: Path) -> Path:
    local = tmp_path / "local"
    repo = Repo.init(local, initial_branch="main")
    (local / "apps").mkdir()
    (local / "apps" / "seed.txt").write_text("seed\n")
    repo.index.add(["apps/seed.txt"])
    repo.index.commit("seed", author=AUTHOR, committer=AUTHOR)
    repo.create_remote("origin", str(remote))
    repo.git.push("origin", "main")
    return local


def _backend(local: Path, *, push_on_write: bool) -> GiteaBackend:
    return GiteaBackend(
        local,
        api_url="http://gitea.example/api/v1",
        owner="kubemend",
        repo="gitops",
        token="test-token",
        push_on_write=push_on_write,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(201, json={"number": 1, "html_url": "http://pr"})
            )
        ),
    )


def _branch_exists_on_remote(remote: Path, name: str) -> bool:
    return name in [h.name for h in Repo(remote).heads]


def test_push_on_write_false_defers_the_push_to_open_draft_pr(
    local_repo: Path, remote: Path
) -> None:
    backend = _backend(local_repo, push_on_write=False)
    branch = backend.open_branch("main", "kubemend/run1")

    backend.write_files(branch, {"apps/shop-api/values.yaml": "replicaCount: 3\n"}, "fix")
    assert not _branch_exists_on_remote(remote, "kubemend/run1"), "today's behavior: no push yet"

    backend.open_draft_pr(branch, "title", "body")
    assert _branch_exists_on_remote(remote, "kubemend/run1")


def test_push_on_write_true_pushes_immediately(local_repo: Path, remote: Path) -> None:
    backend = _backend(local_repo, push_on_write=True)
    branch = backend.open_branch("main", "kubemend/run1")

    backend.write_files(branch, {"apps/shop-api/values.yaml": "replicaCount: 3\n"}, "fix")

    assert _branch_exists_on_remote(remote, "kubemend/run1"), (
        "split mode needs the branch on the remote before the diff stage runs, "
        "not only after a verified proposal"
    )


def test_push_on_write_true_reflects_amendments_from_a_second_write(
    local_repo: Path, remote: Path
) -> None:
    """A model's self-check → amend → self-check loop must see its own latest
    amendment on the remote each time, not a stale first-write revision."""
    backend = _backend(local_repo, push_on_write=True)
    branch = backend.open_branch("main", "kubemend/run1")
    backend.write_files(branch, {"apps/shop-api/values.yaml": "replicaCount: 3\n"}, "first")
    first_sha = Repo(remote).heads["kubemend/run1"].commit.hexsha

    backend.write_files(branch, {"apps/shop-api/values.yaml": "replicaCount: 4\n"}, "amend")
    second_sha = Repo(remote).heads["kubemend/run1"].commit.hexsha

    assert second_sha != first_sha
