"""Read access to the GitOps repository (ARCHITECTURE.md §4.1).

`propose_git_change` demands complete file contents rather than a diff, which is
only a fair request if the model can see what the file currently holds. Without
these tools it must reconstruct values.yaml from memory, and the first lab run
showed exactly what that costs: a correct diagnosis (a nonexistent image tag)
paired with a values file that silently dropped `service.port`, so every render
failed and the run burned its iteration budget re-guessing.

Reads resolve against the **base branch**, never the working tree. The proposer
leaves the checkout on `kubemend/<run_id>`, so a working-tree read would hand the
model back its own last proposal — the second lab run did exactly that, read its
own truncated values.yaml, and concluded the chart's templates were broken. The
base branch is the one fixed point both the model and the reviewer mean by "the
current file".

Read-tier and side-effect free, so they do not touch invariant I5 — the single
write path is still `propose_git_change`. Reads are not restricted to
`writable_globs`: the model needs templates and Chart.yaml to understand which
values the chart actually consumes, and reading them changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from git import GitCommandError, Repo

from kubemend.tools.base import ToolSpec

MAX_BYTES = 64_000


@dataclass
class GitOpsReader:
    repo_path: Path
    base_branch: str = "main"

    def _repo(self) -> Repo:
        return Repo(self.repo_path)

    def _check(self, rel: str) -> str | None:
        """Reject anything that is not a plain repo-relative path.

        Git resolves `base:path` from the tree root, so traversal cannot escape
        the repository the way a filesystem read could — but a path like
        `../x` is still a mistake worth naming rather than a confusing miss.
        """
        if not rel:
            return "a path is required"
        if rel.startswith("/"):
            return f"'{rel}' must be relative to the repository root"
        if ".." in Path(rel).parts:
            return f"'{rel}' must not traverse outside the repository"
        if Path(rel).parts[:1] == (".git",):
            return f"'{rel}' is not readable"
        return None

    def read(self, rel: str) -> dict[str, Any]:
        problem = self._check(rel)
        if problem:
            return {"error": {"type": "path_not_readable", "message": problem}}
        try:
            content = self._repo().git.show(f"{self.base_branch}:{rel}")
        except GitCommandError:
            return {
                "error": {
                    "type": "not_found",
                    "message": f"no file at '{rel}' on {self.base_branch}",
                }
            }
        except Exception as exc:
            return {"error": {"type": "path_not_readable", "message": str(exc)}}

        truncated = len(content.encode()) > MAX_BYTES
        if truncated:
            content = content.encode()[:MAX_BYTES].decode(errors="ignore")
        return {"path": rel, "content": content, "truncated": truncated}

    def list(self, pattern: str) -> dict[str, Any]:
        try:
            listing = self._repo().git.ls_tree("-r", "--name-only", self.base_branch)
        except Exception as exc:
            return {"error": {"type": "not_found", "message": str(exc)}}
        # git already excludes .git; only the glob has to be applied.
        paths = [p for p in listing.splitlines() if p and _matches(p, pattern)]
        return {"pattern": pattern, "paths": sorted(paths)}


def _matches(path: str, pattern: str) -> bool:
    """`**/*` and `apps/**/*` must behave the way the tool description promises."""
    if pattern in ("", "**/*", "**"):
        return True
    return fnmatch(path, pattern) or fnmatch(path, pattern.replace("**/", "*"))


def read_gitops_file_spec(reader: GitOpsReader) -> ToolSpec:
    def _execute(args: dict[str, Any]) -> dict[str, Any]:
        return reader.read(str(args.get("path", "")))

    return ToolSpec(
        name="read_gitops_file",
        description=(
            "Read one file from the GitOps repository as it currently stands on the base "
            "branch. Use this before propose_git_change so the contents you submit are the "
            "current file with your edit applied, rather than a reconstruction — "
            "propose_git_change replaces the whole file, so anything you omit is deleted. "
            "Chart templates and Chart.yaml are readable too, which is how you tell which "
            "values a chart requires."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative path, e.g. apps/shop-api/values.yaml",
                }
            },
            "required": ["path"],
        },
        executor=_execute,
        tier="read",
        timeout_s=10.0,
    )


def list_gitops_files_spec(reader: GitOpsReader) -> ToolSpec:
    def _execute(args: dict[str, Any]) -> dict[str, Any]:
        return reader.list(str(args.get("pattern", "**/*")))

    return ToolSpec(
        name="list_gitops_files",
        description=(
            "List files in the GitOps repository matching a glob, e.g. 'apps/shop-api/**/*' "
            "to see a chart's layout before reading its templates."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob relative to the repository root; defaults to **/*",
                }
            },
            "required": [],
        },
        executor=_execute,
        tier="read",
        timeout_s=10.0,
    )
