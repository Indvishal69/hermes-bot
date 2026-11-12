"""GitHub connector: repos, files, issues and pull requests."""

from __future__ import annotations

import base64
from typing import Any

from .base import Connector, ConnectorError, ConnectorTool, json_list, safe_path, safe_repo


class GitHubConnector(Connector):
    name = "github"
    title = "GitHub"
    description = "Repositories, branches, files, issues and pull requests."
    env_vars = ("GITHUB_TOKEN",)
    docs_url = "https://docs.github.com/en/rest"
    settings_schema = {
        "default_repo": "owner/name used when a tool call omits 'repo'",
    }
    base = "https://api.github.com"

    # -- helpers ------------------------------------------------------------
    def _client(self):
        return self.client(
            self.base,
            {
                "Authorization": f"Bearer {self.credential('GITHUB_TOKEN')}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _repo(self, repo: str = "") -> str:
        return safe_repo(repo or self.require_setting("default_repo", "repository"))

    # -- read tools ---------------------------------------------------------
    def whoami(self) -> str:
        data = self._client().request("GET", "/user")
        return f"Signed in as {data.get('login')} ({data.get('name') or 'no name'}), " \
               f"{data.get('public_repos', 0)} public repos."

    def list_repos(self, limit: str = "10") -> str:
        count = max(1, min(int(limit or 10), 50))
        data = self._client().request(
            "GET", "/user/repos", params={"per_page": count, "sort": "updated"}
        )
        return json_list(
            [f"{item['full_name']} (private={item['private']}, {item['default_branch']})"
             for item in data]
        )

    def get_repo(self, repo: str = "") -> str:
        data = self._client().request("GET", f"/repos/{self._repo(repo)}")
        return (
            f"{data['full_name']} — default branch {data['default_branch']}, "
            f"language {data.get('language')}, stars {data.get('stargazers_count')}, "
            f"open issues {data.get('open_issues_count')}, private={data['private']}"
        )

    def list_branches(self, repo: str = "") -> str:
        data = self._client().request(
            "GET", f"/repos/{self._repo(repo)}/branches", params={"per_page": 100}
        )
        return json_list([f"{item['name']} @ {item['commit']['sha'][:8]}" for item in data])

    def get_file(self, path: str = "", repo: str = "", ref: str = "main") -> str:
        target = safe_path(path or "README.md")
        data = self._client().request(
            "GET", f"/repos/{self._repo(repo)}/contents/{target}", params={"ref": ref or "main"}
        )
        if isinstance(data, list):
            return json_list([f"{item['type']}: {item['path']}" for item in data])
        content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        return f"{data.get('path')} @ {ref} ({data.get('size')} bytes)\n{content[: self.max_chars]}"

    def list_issues(self, repo: str = "", state: str = "open") -> str:
        data = self._client().request(
            "GET",
            f"/repos/{self._repo(repo)}/issues",
            params={"state": state if state in ("open", "closed", "all") else "open", "per_page": 25},
        )
        rows = [
            f"#{item['number']} [{item['state']}] {item['title']}"
            for item in data
            if "pull_request" not in item
        ]
        return json_list(rows)

    def list_pulls(self, repo: str = "", state: str = "open") -> str:
        data = self._client().request(
            "GET",
            f"/repos/{self._repo(repo)}/pulls",
            params={"state": state if state in ("open", "closed", "all") else "open", "per_page": 25},
        )
        return json_list([f"#{item['number']} [{item['state']}] {item['title']}" for item in data])

    # -- write tools (owner approval required) ------------------------------
    def create_issue(self, title: str = "", body: str = "", repo: str = "") -> str:
        if not (title or "").strip():
            raise ConnectorError("github_create_issue needs a title")
        data = self._client().request(
            "POST",
            f"/repos/{self._repo(repo)}/issues",
            json_body={"title": title[:250], "body": (body or "")[:60_000]},
        )
        return f"Created issue #{data.get('number')}: {data.get('html_url')}"

    def commit_file(
        self, path: str = "", content: str = "", message: str = "", branch: str = ""
    ) -> str:
        target = safe_path(path)
        if not content:
            raise ConnectorError("github_commit_file needs content")
        repo = self._repo()
        client = self._client()
        existing: dict[str, Any] | None = None
        try:
            current = client.request(
                "GET", f"/repos/{repo}/contents/{target}", params={"ref": branch or "main"}
            )
            if isinstance(current, dict):
                existing = current
        except ConnectorError:
            existing = None  # new file
        payload: dict[str, Any] = {
            "message": (message or f"Update {target} via Hermes")[:200],
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if branch:
            payload["branch"] = branch
        if existing and existing.get("sha"):
            payload["sha"] = existing["sha"]
        data = client.request(
            "PUT", f"/repos/{repo}/contents/{target}", json_body=payload
        )
        commit = data.get("commit", {})
        return f"Committed {target} to {repo} ({commit.get('sha', '')[:8]}): {commit.get('html_url', '')}"

    # -- registry -----------------------------------------------------------
    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool("github_whoami", "Show the GitHub account the token belongs to.",
                          handler=self.whoami),
            ConnectorTool("github_list_repos", "List repositories for the token's user.",
                          {"limit": "int (optional, default 10)"}, self.list_repos),
            ConnectorTool("github_get_repo", "Repository summary (branch, language, stars).",
                          {"repo": "str 'owner/name' (optional if default_repo saved)"}, self.get_repo),
            ConnectorTool("github_list_branches", "List branches of a repository.",
                          {"repo": "str (optional)"}, self.list_branches),
            ConnectorTool("github_get_file", "Read a text file from a repository.",
                          {"path": "str (required)", "repo": "str (optional)",
                           "ref": "str (optional, default main)"}, self.get_file),
            ConnectorTool("github_list_issues", "List issues (pull requests excluded).",
                          {"repo": "str (optional)", "state": "open|closed|all"}, self.list_issues),
            ConnectorTool("github_list_pulls", "List pull requests.",
                          {"repo": "str (optional)", "state": "open|closed|all"}, self.list_pulls),
            ConnectorTool("github_create_issue", "Create an issue in a repository.",
                          {"title": "str (required)", "body": "str (optional)",
                           "repo": "str (optional)"}, self.create_issue, write=True),
            ConnectorTool("github_commit_file",
                          "Create or update one file in a repository with a commit.",
                          {"path": "str (required)", "content": "str (required, full file)",
                           "message": "str (optional commit message)",
                           "branch": "str (optional, default main)"},
                          self.commit_file, write=True),
        ]

    def check(self) -> str:
        return self.whoami()
