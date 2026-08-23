"""Gitea backend (ARCHITECTURE.md §4.2).

Real draft PRs against the in-cluster Gitea, for the full Argo-visible demo. A
GitHubBackend is the same shape and lands when there is a reason for it.

Composition rather than inheritance: the local backend already does branch and
commit correctly, so this adds exactly two things — a push of that branch, and
the PR call. The base branch is never a push target, which is the other half of
invariant I5.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from git import GitCommandError

from kubemend.tools.base import ClientError, TransportError
from kubemend.tools.gitops.backend import Branch, Commit, PrRef
from kubemend.tools.gitops.local_backend import LocalGitBackend

# Gitea has no draft flag on its create-PR API: a pull request is a draft if and
# only if its title carries this prefix. Sending draft=True in the payload is
# silently ignored, which is how the first lab PR came out review-ready.
WIP_PREFIX = "WIP:"


class GiteaBackend:
    def __init__(
        self,
        repo_path: Path | str,
        *,
        api_url: str,
        owner: str,
        repo: str,
        token: str,
        remote: str = "origin",
        client: httpx.Client | None = None,
        # M11 split mode: the Argo CD multi-source diff (`--revisions`) reads a
        # pushed ref, not the local working tree, unlike single-repo's `--local`.
        # Off by default — single-repo mode keeps today's behavior of pushing
        # only once a proposal is verified (open_draft_pr), never mid-loop.
        push_on_write: bool = False,
    ) -> None:
        self._local = LocalGitBackend(repo_path)
        self.api_url = api_url.rstrip("/")
        self.owner = owner
        self.repo = repo
        self.remote = remote
        self.push_on_write = push_on_write
        self._client = client or httpx.Client(
            timeout=20.0,
            # Header auth rather than credentials in the remote URL: a URL leaks
            # into .git/config, into error output, and into any log that echoes
            # the command.
            headers={"Authorization": f"token {token}"},
        )

    def open_branch(self, base: str, name: str) -> Branch:
        return self._local.open_branch(base, name)

    def write_files(self, branch: Branch, files: dict[str, str], message: str) -> Commit:
        commit = self._local.write_files(branch, files, message)
        if self.push_on_write:
            self._push(branch)
        return commit

    def _push(self, branch: Branch) -> None:
        if branch.name == branch.base:
            raise ClientError(f"refusing to push the base branch '{branch.base}'")
        try:
            self._local.repo.git.push(self.remote, f"{branch.name}:{branch.name}", "--force")
        except GitCommandError as exc:
            raise TransportError(f"could not push {branch.name}: {exc}") from exc

    def open_draft_pr(self, branch: Branch, title: str, body: str) -> PrRef:
        if not self.push_on_write:
            self._push(branch)

        payload = {
            "head": branch.name,
            "base": branch.base,
            "title": _as_draft(title),
            "body": body,
        }
        url = f"{self.api_url}/repos/{self.owner}/{self.repo}/pulls"
        try:
            response = self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise TransportError(f"gitea unreachable: {exc}") from exc

        if response.status_code == 409:
            # A PR for this branch already exists, which is the expected state
            # when the model amends its proposal. Not an error.
            return PrRef(ref=branch.name, url=self._existing_pr_url(branch), draft=True)
        if response.status_code >= 500:
            raise TransportError(f"gitea returned {response.status_code}")
        if response.status_code >= 400:
            raise ClientError(f"gitea rejected the pull request: {response.text[:300]}")

        body_json = response.json()
        return PrRef(
            ref=str(body_json.get("number", branch.name)),
            url=str(body_json.get("html_url", "")),
            draft=True,
        )

    def _existing_pr_url(self, branch: Branch) -> str:
        url = f"{self.api_url}/repos/{self.owner}/{self.repo}/pulls"
        try:
            response = self._client.get(url, params={"state": "open"})
            for pr in response.json():
                if pr.get("head", {}).get("ref") == branch.name:
                    return str(pr.get("html_url", ""))
        except (httpx.HTTPError, ValueError):
            pass
        return ""


def _as_draft(title: str) -> str:
    """Mark the title so gitea files the PR as a draft, without double-prefixing."""
    if title.upper().startswith(WIP_PREFIX):
        return title
    return f"{WIP_PREFIX} {title}"
