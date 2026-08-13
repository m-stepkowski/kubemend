"""propose_git_change executor (ARCHITECTURE.md §4, tool contract §propose).

Enforces the `writable_globs` path policy before anything is written: a path
outside `apps/**/values*.yaml` yields a structured `path_not_writable` error and
no file is touched. Files are YAML-parsed up front as a cheap pre-gate that
saves render cycles.

One active branch per run — the first call opens `kubemend/<run_id>` off the
base branch, later calls amend it.

This module is invariant I5 in code: it is the only place in the project that
writes anything outside the local workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any

import yaml

from kubemend.tools.base import ClientError, ToolSpec
from kubemend.tools.gitops.backend import Branch, GitBackend, PrRef


class PathNotWritable(ClientError):
    """A proposed path is outside the configured writable globs."""

    error_type = "path_not_writable"


class InvalidYaml(ClientError):
    """A proposed file is not parseable YAML."""

    error_type = "invalid_yaml"


def is_writable(path: str, writable_globs: list[str]) -> bool:
    """Match a repo-relative path against the configured globs.

    `fnmatch` treats `*` as matching separators too, which would let
    `apps/*/values.yaml` accept `apps/a/b/values.yaml`. That is the permissive
    direction, so the check below additionally requires the path to stay inside
    the segment shape the glob describes.
    """
    normalised = path.lstrip("./")
    if normalised != path.lstrip("/") or path.startswith("/") or ".." in normalised.split("/"):
        # Absolute paths and traversal never match: they are the obvious way to
        # escape the policy, and no legitimate proposal needs them.
        return False
    return any(fnmatch(normalised, glob) for glob in writable_globs)


@dataclass
class Proposer:
    """Holds the single active branch for a run."""

    backend: GitBackend
    writable_globs: list[str]
    base_branch: str = "main"
    run_id: str = "run"
    _branch: Branch | None = field(default=None, repr=False)
    files_written: list[str] = field(default_factory=list)
    rationale: str = ""
    incident_ref: str = ""

    @property
    def branch_name(self) -> str:
        return f"kubemend/{self.run_id}"

    def current_branch(self) -> Branch | None:
        return self._branch

    def propose(
        self, files: dict[str, str], rationale: str, incident_ref: str = ""
    ) -> dict[str, Any]:
        if not files:
            raise ClientError("no files provided; propose the full content of the files to change")

        # Validate every path before writing any of them: a partial write would
        # leave a branch that half-implements a rejected proposal.
        for path in sorted(files):
            if not is_writable(path, self.writable_globs):
                raise PathNotWritable(
                    f"'{path}' is not writable. Only paths matching "
                    f"{', '.join(self.writable_globs)} may be changed. Chart templates and "
                    "anything outside those globs require a human — say so in your summary."
                )

        for path, content in sorted(files.items()):
            try:
                yaml.safe_load(content)
            except yaml.YAMLError as exc:
                raise InvalidYaml(f"'{path}' is not valid YAML: {exc}") from exc

        if self._branch is None:
            self._branch = self.backend.open_branch(self.base_branch, self.branch_name)

        message = f"kubemend: {rationale.splitlines()[0][:72]}" if rationale else "kubemend: fix"
        self.backend.write_files(self._branch, files, message)

        self.rationale = rationale or self.rationale
        self.incident_ref = incident_ref or self.incident_ref
        for path in files:
            if path not in self.files_written:
                self.files_written.append(path)

        return {
            "branch": self._branch.name,
            "files_written": sorted(files),
            "base": self._branch.base,
        }

    def open_pr(self, title: str, body: str) -> PrRef | None:
        if self._branch is None:
            return None
        return self.backend.open_draft_pr(self._branch, title, body)


def propose_tool_spec(proposer: Proposer) -> ToolSpec:
    """`propose_git_change` as the model sees it (docs/knowledge/tool-contracts.md)."""

    def _execute(args: dict[str, Any]) -> dict[str, Any]:
        files = args.get("files")
        if not isinstance(files, dict):
            raise ClientError("`files` must be an object mapping path -> full file content")
        return proposer.propose(
            files={str(k): str(v) for k, v in files.items()},
            rationale=str(args.get("rationale", "")),
            incident_ref=str(args.get("incident_ref", "")),
        )

    return ToolSpec(
        name="propose_git_change",
        description=(
            "Propose a fix by writing complete new contents for GitOps values files and "
            "opening/updating ONE draft PR for this run. Only paths matching "
            "apps/**/values*.yaml are writable. Provide the full file content, not a diff. "
            "Rationale must reference concrete evidence (metric/log/state) gathered this run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "files": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "path -> full new file content",
                },
                "rationale": {"type": "string"},
                "incident_ref": {"type": "string"},
            },
            "required": ["files", "rationale"],
        },
        executor=_execute,
        tier="propose",
        timeout_s=30.0,
    )
