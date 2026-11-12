"""GitHub REST integration (read + explicit writes, never automatic)."""

from __future__ import annotations

import base64
from typing import Any

from .base import Integration, IntegrationError


class GitHubIntegration(Integration):
    name = "github"
    env_var = "GITHUB_TOKEN"
    base_url = "https://api.github.com"

    def whoami(self) -> dict[str, Any]:
        return self.request("GET", "/user")

    def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return self.request("GET", f"/repos/{owner}/{repo}")

    def list_branches(self, owner: str, repo: str) -> list[str]:
        data = self.request("GET", f"/repos/{owner}/{repo}/branches", params={"per_page": 100})
        return [item.get("name", "") for item in data if isinstance(item, dict)]

    def get_file(self, owner: str, repo: str, path: str, ref: str = "main") -> str:
        data = self.request(
            "GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref}
        )
        if isinstance(data, list):
            raise IntegrationError(f"{path} is a directory")
        content = data.get("content", "")
        return base64.b64decode(content).decode("utf-8", errors="replace")

    def default_branch(self, owner: str, repo: str) -> str:
        return self.get_repo(owner, repo).get("default_branch", "main")

    def head_sha(self, owner: str, repo: str, branch: str) -> str:
        data = self.request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
        return data["object"]["sha"]

    def create_branch(self, owner: str, repo: str, branch: str, from_branch: str) -> str:
        sha = self.head_sha(owner, repo, from_branch)
        self.request(
            "POST",
            f"/repos/{owner}/{repo}/git/refs",
            json_body={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        return sha

    def put_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        *,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        return self.request("PUT", f"/repos/{owner}/{repo}/contents/{path}", json_body=body)

    def open_pull_request(
        self, owner: str, repo: str, *, title: str, head: str, base: str, body: str = ""
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json_body={"title": title, "head": head, "base": base, "body": body},
        )
