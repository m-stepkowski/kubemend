"""Local git backend (ARCHITECTURE.md §4.2).

Plain git against a local clone; the "PR" is a branch plus a generated
PROPOSAL.md. Used in CI and most lab runs, where a real forge would be
gratuitous.

Two safety properties this backend upholds structurally rather than by
convention, because it is one half of invariant I5:

* it never checks out or commits to the base branch — every write happens on a
  `kubemend/<run_id>` branch created off it;
* it never pushes. A local branch is inert until a human looks at it.
"""

from __future__ import annotations

from pathlib import Path

from git import Actor, GitCommandError, Repo

from kubemend.tools.base import ClientError, TransportError
from kubemend.tools.gitops.backend import Branch, Commit, PrRef

PROPOSAL_FILE = "PROPOSAL.md"

AUTHOR_NAME = "kubemend"
AUTHOR_EMAIL = "kubemend@localhost"


class LocalGitBackend:
    def __init__(self, repo_path: Path | str) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        try:
            self.repo = Repo(self.repo_path)
        except Exception as exc:
            raise ClientError(f"'{self.repo_path}' is not a git repository: {exc}") from exc

    def open_branch(self, base: str, name: str) -> Branch:
        if name == base:
            # The one thing this class must never do.
            raise ClientError(f"refusing to write to the base branch '{base}'")
        try:
            self.repo.git.checkout(base)
            existing = [h.name for h in self.repo.heads]
            if name in existing:
                self.repo.git.checkout(name)
            else:
                self.repo.git.checkout("-b", name)
        except GitCommandError as exc:
            raise TransportError(f"could not open branch {name}: {exc}") from exc
        return Branch(name=name, base=base)

    def write_files(self, branch: Branch, files: dict[str, str], message: str) -> Commit:
        if branch.name == branch.base:
            raise ClientError(f"refusing to commit to the base branch '{branch.base}'")

        written = []
        for rel, content in sorted(files.items()):
            target = (self.repo_path / rel).resolve()
            # Belt and braces over the proposer's path policy: a path that
            # escapes the repository must never reach the filesystem, whatever
            # the caller believed it had validated.
            if not target.is_relative_to(self.repo_path):
                raise ClientError(f"'{rel}' resolves outside the repository")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            written.append(str(target.relative_to(self.repo_path)))

        try:
            self.repo.index.add(written)
            commit = self.repo.index.commit(
                message,
                author=_actor(),
                committer=_actor(),
            )
        except GitCommandError as exc:
            raise TransportError(f"commit failed: {exc}") from exc
        return Commit(sha=commit.hexsha, message=message)

    def open_draft_pr(self, branch: Branch, title: str, body: str) -> PrRef:
        """No forge here, so the review artefact is a file on the branch."""
        proposal = self.repo_path / PROPOSAL_FILE
        proposal.write_text(f"# {title}\n\n{body}\n")
        self.repo.index.add([PROPOSAL_FILE])
        self.repo.index.commit(f"kubemend: {title}", author=_actor(), committer=_actor())
        return PrRef(ref=branch.name, url=f"file://{proposal}", draft=True)


def _actor() -> Actor:
    return Actor(AUTHOR_NAME, AUTHOR_EMAIL)
