"""Path policy on the single write path (ARCHITECTURE.md §4.1, invariant I5).

The first M3 acceptance item: an attempt to write a chart template must produce
a structured error and leave nothing written. This is the control that keeps the
agent's blast radius inside values files, so it is tested from both directions —
what it permits and what it refuses — including the paths someone would use to
deliberately escape it.
"""

from __future__ import annotations

import pytest

from kubemend.core.model import ToolCall
from kubemend.tools.gitops.backend import Branch, Commit, PrRef
from kubemend.tools.gitops.proposer import (
    InvalidYaml,
    PathNotWritable,
    Proposer,
    is_writable,
    propose_tool_spec,
)
from kubemend.tools.registry import ToolRegistry

GLOBS = ["apps/**/values*.yaml"]


class RecordingBackend:
    """Records what it was asked to do, so tests can assert nothing was written."""

    def __init__(self) -> None:
        self.branches: list[Branch] = []
        self.writes: list[dict[str, str]] = []
        self.prs: list[tuple[str, str]] = []

    def open_branch(self, base: str, name: str) -> Branch:
        branch = Branch(name=name, base=base)
        self.branches.append(branch)
        return branch

    def write_files(self, branch: Branch, files: dict[str, str], message: str) -> Commit:
        self.writes.append(dict(files))
        return Commit(sha="deadbeef", message=message)

    def open_draft_pr(self, branch: Branch, title: str, body: str) -> PrRef:
        self.prs.append((title, body))
        return PrRef(ref=branch.name, url=f"local://{branch.name}", draft=True)


def _proposer() -> tuple[Proposer, RecordingBackend]:
    backend = RecordingBackend()
    return Proposer(backend=backend, writable_globs=GLOBS, run_id="abc123"), backend


# -- the glob itself ------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "apps/shop-api/values.yaml",
        "apps/shop-api/values-lab.yaml",
        "apps/shop-worker/values.yaml",
    ],
)
def test_values_files_are_writable(path: str) -> None:
    assert is_writable(path, GLOBS) is True


@pytest.mark.parametrize(
    "path",
    [
        "apps/shop-api/templates/deployment.yaml",  # the acceptance case
        "apps/shop-api/Chart.yaml",
        "argocd/apps/shop-api.yaml",
        "policies/disallow-privileged.yaml",
        "Taskfile.yaml",
        "../../etc/passwd",
        "/etc/passwd",
        "apps/../../../etc/passwd",
        "apps/shop-api/values.yaml/../../../Taskfile.yaml",
    ],
)
def test_everything_else_is_refused(path: str) -> None:
    assert is_writable(path, GLOBS) is False


# -- the executor ---------------------------------------------------------


def test_writing_a_template_is_refused_and_nothing_is_written() -> None:
    proposer, backend = _proposer()

    with pytest.raises(PathNotWritable) as exc:
        proposer.propose(
            files={"apps/shop-api/templates/deployment.yaml": "kind: Deployment\n"},
            rationale="raise the memory limit",
        )

    assert exc.value.error_type == "path_not_writable"
    assert backend.writes == [], "no file may be written when any path is refused"
    assert backend.branches == [], "a refused proposal must not even open a branch"


def test_a_single_bad_path_rejects_the_whole_proposal() -> None:
    """Partial application would leave a branch half-implementing a rejected fix."""
    proposer, backend = _proposer()

    with pytest.raises(PathNotWritable):
        proposer.propose(
            files={
                "apps/shop-api/values.yaml": "replicaCount: 3\n",
                "apps/shop-api/templates/deployment.yaml": "kind: Deployment\n",
            },
            rationale="two changes, one of them illegal",
        )

    assert backend.writes == []


def test_valid_proposal_opens_one_branch_and_writes() -> None:
    proposer, backend = _proposer()

    payload = proposer.propose(
        files={"apps/shop-api/values.yaml": "replicaCount: 3\n"},
        rationale="scale out to absorb the traffic spike",
        incident_ref="INC-42",
    )

    assert payload["branch"] == "kubemend/abc123"
    assert payload["files_written"] == ["apps/shop-api/values.yaml"]
    assert len(backend.branches) == 1
    assert backend.writes == [{"apps/shop-api/values.yaml": "replicaCount: 3\n"}]


def test_repeated_proposals_amend_the_same_branch() -> None:
    """One active branch per run (§4): a second call must not open another."""
    proposer, backend = _proposer()

    proposer.propose({"apps/shop-api/values.yaml": "replicaCount: 3\n"}, "first")
    proposer.propose({"apps/shop-api/values-lab.yaml": "replicaCount: 4\n"}, "second")

    assert len(backend.branches) == 1
    assert len(backend.writes) == 2
    assert proposer.files_written == [
        "apps/shop-api/values.yaml",
        "apps/shop-api/values-lab.yaml",
    ]


def test_invalid_yaml_is_caught_before_the_render_cycle() -> None:
    """A cheap pre-gate: catching this here saves a helm render round trip."""
    proposer, backend = _proposer()

    with pytest.raises(InvalidYaml) as exc:
        proposer.propose({"apps/shop-api/values.yaml": "replicaCount: [unclosed\n"}, "oops")

    assert exc.value.error_type == "invalid_yaml"
    assert backend.writes == []


def test_error_reaches_the_model_as_a_payload_not_an_exception() -> None:
    """I2 end to end: the registry turns the refusal into data for the model."""
    proposer, backend = _proposer()
    registry = ToolRegistry([propose_tool_spec(proposer)])

    outcome = registry.execute(
        ToolCall(
            id="c1",
            name="propose_git_change",
            arguments={
                "files": {"apps/shop-api/templates/deployment.yaml": "kind: Deployment\n"},
                "rationale": "raise the limit",
            },
        )
    )

    assert outcome.ok is False
    assert outcome.payload["error"]["type"] == "path_not_writable"
    assert "apps/**/values*.yaml" in outcome.payload["error"]["detail"]
    assert backend.writes == []
