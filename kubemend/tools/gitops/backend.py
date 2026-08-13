"""GitBackend Protocol (ARCHITECTURE.md §4.2).

Three operations — open a branch, write files, open a draft PR. Deliberately
narrow: there is no method here that could push to a protected branch or touch a
cluster, which is what makes invariant I5 hold by construction.

Two implementations satisfy it: `LocalGitBackend` (a plain clone; the "PR" is a
branch plus a generated PROPOSAL.md) and `GiteaBackend` (a real draft PR). The
seam exists so CI and most lab runs never need a forge, while the demo can show
a genuine PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Branch:
    name: str
    base: str


@dataclass(frozen=True)
class Commit:
    sha: str
    message: str


@dataclass(frozen=True)
class PrRef:
    """Where a human goes to review the proposal.

    `url` is a real PR link for a forge backend, and a filesystem path for the
    local one; callers treat it as an opaque reference for the run result.
    """

    ref: str
    url: str
    draft: bool = True


class GitBackend(Protocol):
    def open_branch(self, base: str, name: str) -> Branch: ...

    def write_files(self, branch: Branch, files: dict[str, str], message: str) -> Commit: ...

    def open_draft_pr(self, branch: Branch, title: str, body: str) -> PrRef: ...
