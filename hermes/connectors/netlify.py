"""Netlify connector: sites, deploys, trigger a build, cancel a deploy."""

from __future__ import annotations

from typing import Any

from .base import Connector, ConnectorError, ConnectorTool, json_list, safe_id


class NetlifyConnector(Connector):
    name = "netlify"
    title = "Netlify"
    description = "Sites, deploys, trigger a build, cancel a deploy."
    env_vars = ("NETLIFY_TOKEN",)
    docs_url = "https://docs.netlify.com/api/get-started/"
    settings_schema = {
        "default_site": "site id or site name used when a tool call omits 'site'",
    }
    base = "https://api.netlify.com/api/v1"

    def _client(self):
        return self.client(
            self.base,
            {
                "Authorization": f"Bearer {self.credential('NETLIFY_TOKEN')}",
                "Content-Type": "application/json",
            },
        )

    def _site(self, site: str = "") -> str:
        # Netlify accepts either the site id or its name in these paths.
        return safe_id(site or self.require_setting("default_site", "site id or name"), what="site")

    # -- read tools ---------------------------------------------------------
    def list_sites(self, limit: str = "10") -> str:
        count = max(1, min(int(limit or 10), 100))
        data = self._client().request("GET", "/sites", params={"per_page": count})
        rows = []
        for site in data if isinstance(data, list) else []:
            rows.append(
                f"{site.get('name')} ({site.get('id')}) — "
                f"{site.get('ssl_url') or site.get('url')} "
                f"[repo: {(site.get('build_settings') or {}).get('repo_url') or 'manual'}]"
            )
        return json_list(rows)

    def get_site(self, site: str = "") -> str:
        data = self._client().request("GET", f"/sites/{self._site(site)}")
        settings = data.get("build_settings") or {}
        return (
            f"{data.get('name')} ({data.get('id')})\n"
            f"url: {data.get('ssl_url') or data.get('url')}\n"
            f"admin: {data.get('admin_url')}\n"
            f"repo: {settings.get('repo_url') or 'manual deploys'} "
            f"branch: {settings.get('repo_branch') or settings.get('branch') or '-'}\n"
            f"build command: {settings.get('cmd') or '-'} · publish dir: {settings.get('dir') or '-'}"
        )

    def list_deploys(self, site: str = "", limit: str = "10") -> str:
        count = max(1, min(int(limit or 10), 50))
        data = self._client().request(
            "GET", f"/sites/{self._site(site)}/deploys", params={"per_page": count}
        )
        rows = []
        for deploy in data if isinstance(data, list) else []:
            rows.append(
                f"{deploy.get('id')} state={deploy.get('state')} "
                f"branch={deploy.get('branch') or '-'} "
                f"context={deploy.get('context') or '-'} "
                f"created={deploy.get('created_at')} "
                f"{deploy.get('deploy_ssl_url') or ''}"
            )
        return json_list(rows)

    def get_deploy(self, deploy_id: str = "") -> str:
        target = safe_id(deploy_id, what="deploy id")
        data = self._client().request("GET", f"/deploys/{target}")
        lines = [
            f"{data.get('id')} state={data.get('state')}",
            f"branch={data.get('branch') or '-'} commit={data.get('commit_ref') or '-'}",
            f"url: {data.get('deploy_ssl_url') or data.get('url') or '-'}",
            f"created={data.get('created_at')} published={data.get('published_at') or '-'}",
        ]
        if data.get("error_message"):
            lines.append(f"error: {data['error_message']}")
        return "\n".join(lines)

    # -- write tools (owner approval required) ------------------------------
    def trigger_build(self, site: str = "") -> str:
        target = self._site(site)
        data = self._client().request("POST", f"/sites/{target}/builds", json_body={})
        deploy_id = data.get("deploy_id") or data.get("id") or ""
        return f"Build triggered for {target} (deploy {deploy_id or 'queued'})."

    def cancel_deploy(self, deploy_id: str = "") -> str:
        target = safe_id(deploy_id, what="deploy id")
        self._client().request("POST", f"/deploys/{target}/cancel")
        return f"Cancellation requested for deploy {target}."

    # -- registry -----------------------------------------------------------
    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool("netlify_list_sites", "List Netlify sites.",
                          {"limit": "int (optional, default 10)"}, self.list_sites),
            ConnectorTool("netlify_get_site", "Site detail (URLs, repo, build settings).",
                          {"site": "str (optional if default_site saved)"}, self.get_site),
            ConnectorTool("netlify_list_deploys", "Recent deploys of a site.",
                          {"site": "str (optional)", "limit": "int (optional, default 10)"},
                          self.list_deploys),
            ConnectorTool("netlify_get_deploy", "Detail of one deploy.",
                          {"deploy_id": "str (required)"}, self.get_deploy),
            ConnectorTool("netlify_trigger_build", "Trigger a new build/deploy for a site.",
                          {"site": "str (optional)"}, self.trigger_build, write=True),
            ConnectorTool("netlify_cancel_deploy", "Cancel a running deploy.",
                          {"deploy_id": "str (required)"}, self.cancel_deploy, write=True),
        ]

    def check(self) -> str:
        data: Any = self._client().request("GET", "/sites", params={"per_page": 1})
        count = len(data) if isinstance(data, list) else 0
        return f"Netlify token works ({count} site(s) visible in this page)."
