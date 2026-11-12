"""Deployment and platform connectors: Render, Vercel, Cloudflare, Hugging Face."""

from __future__ import annotations

from .base import Connector, ConnectorError, ConnectorTool, json_list, safe_id


class RenderConnector(Connector):
    name = "render"
    title = "Render"
    description = "Services and deploys on Render."
    env_vars = ("RENDER_API_KEY",)
    docs_url = "https://api-docs.render.com/"
    settings_schema = {"default_service": "service id used when a tool call omits 'service_id'"}
    base = "https://api.render.com/v1"

    def _client(self):
        return self.client(
            self.base,
            {"Authorization": f"Bearer {self.credential('RENDER_API_KEY')}"},
        )

    def list_services(self) -> str:
        data = self._client().request("GET", "/services", params={"limit": 50})
        rows = []
        for entry in data:
            service = entry.get("service", entry) if isinstance(entry, dict) else {}
            rows.append(
                f"{service.get('name')} ({service.get('id')}) type={service.get('type')} "
                f"plan={service.get('serviceDetails', {}).get('plan', '?')}"
            )
        return json_list(rows)

    def list_deploys(self, service_id: str = "") -> str:
        target = safe_id(service_id or self.require_setting("default_service", "service id"))
        data = self._client().request("GET", f"/services/{target}/deploys", params={"limit": 10})
        rows = []
        for entry in data:
            deploy = entry.get("deploy", entry) if isinstance(entry, dict) else {}
            rows.append(
                f"{deploy.get('id')} status={deploy.get('status')} "
                f"created={deploy.get('createdAt')} commit={deploy.get('commit', {}).get('id', '')[:8]}"
            )
        return json_list(rows)

    def trigger_deploy(self, service_id: str = "", clear_cache: str = "false") -> str:
        target = safe_id(service_id or self.require_setting("default_service", "service id"))
        data = self._client().request(
            "POST",
            f"/services/{target}/deploys",
            json_body={"clearCache": "clear" if str(clear_cache).lower() in ("1", "true", "yes") else "do_not_clear"},
        )
        return f"Deploy triggered: {data.get('id')} status={data.get('status')}"

    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool("render_list_services", "List Render services.", {}, self.list_services),
            ConnectorTool("render_list_deploys", "Recent deploys of a service.",
                          {"service_id": "str (optional if default_service saved)"}, self.list_deploys),
            ConnectorTool("render_trigger_deploy", "Trigger a deploy for a service.",
                          {"service_id": "str (optional)", "clear_cache": "true|false (optional)"},
                          self.trigger_deploy, write=True),
        ]

    def check(self) -> str:
        data = self._client().request("GET", "/services", params={"limit": 1})
        return f"Render API reachable ({len(data)} service(s) visible)."


class VercelConnector(Connector):
    name = "vercel"
    title = "Vercel"
    description = "Projects and deployments on Vercel."
    env_vars = ("VERCEL_TOKEN",)
    docs_url = "https://vercel.com/docs/rest-api"
    base = "https://api.vercel.com"

    def _client(self):
        return self.client(
            self.base, {"Authorization": f"Bearer {self.credential('VERCEL_TOKEN')}"}
        )

    def list_projects(self) -> str:
        data = self._client().request("GET", "/v9/projects", params={"limit": 20})
        return json_list([f"{item.get('name')} ({item.get('id')})" for item in data.get("projects", [])])

    def list_deployments(self, limit: str = "10") -> str:
        count = max(1, min(int(limit or 10), 50))
        data = self._client().request("GET", "/v6/deployments", params={"limit": count})
        rows = []
        for item in data.get("deployments", []):
            state = item.get("readyState") or item.get("state")
            rows.append(f"{item.get('uid')} {item.get('name')} state={state} url={item.get('url')}")
        return json_list(rows)

    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool("vercel_list_projects", "List Vercel projects.", {}, self.list_projects),
            ConnectorTool("vercel_list_deployments", "List recent Vercel deployments.",
                          {"limit": "int (optional, default 10)"}, self.list_deployments),
        ]

    def check(self) -> str:
        data = self._client().request("GET", "/v9/projects", params={"limit": 1})
        return f"Vercel API reachable ({len(data.get('projects', []))} project(s) visible)."


class CloudflareConnector(Connector):
    name = "cloudflare"
    title = "Cloudflare"
    description = "Verify the API token and purge cache for a zone."
    env_vars = ("CLOUDFLARE_API_TOKEN",)
    docs_url = "https://developers.cloudflare.com/api/"
    settings_schema = {"default_zone": "zone id used when a tool call omits 'zone_id'"}
    base = "https://api.cloudflare.com/client/v4"

    def _client(self):
        return self.client(
            self.base,
            {"Authorization": f"Bearer {self.credential('CLOUDFLARE_API_TOKEN')}"},
        )

    def verify(self) -> str:
        data = self._client().request("GET", "/user/tokens/verify")
        result = data.get("result", {}) if isinstance(data, dict) else {}
        return f"Token status: {result.get('status', 'unknown')}"

    def purge_cache(self, zone_id: str = "", urls: str = "") -> str:
        target = safe_id(zone_id or self.require_setting("default_zone", "zone id"))
        files = [line.strip() for line in (urls or "").split(",") if line.strip()]
        body = {"files": files} if files else {"purge_everything": True}
        self._client().request("POST", f"/zones/{target}/purge_cache", json_body=body)
        return f"Purged {'everything' if not files else f'{len(files)} URL(s)'} for zone {target}."

    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool("cloudflare_verify_token", "Check that the API token is active.", {}, self.verify),
            ConnectorTool("cloudflare_purge_cache", "Purge a zone's cache (all, or listed URLs).",
                          {"zone_id": "str (optional if default_zone saved)",
                           "urls": "comma separated absolute URLs (optional)"},
                          self.purge_cache, write=True),
        ]

    def check(self) -> str:
        return self.verify()


class HuggingFaceConnector(Connector):
    name = "huggingface"
    title = "Hugging Face"
    description = "Account info and model search."
    env_vars = ("HF_TOKEN",)
    docs_url = "https://huggingface.co/docs/api-inference/"
    base = "https://huggingface.co/api"

    def _client(self):
        return self.client(
            self.base, {"Authorization": f"Bearer {self.credential('HF_TOKEN')}"}
        )

    def whoami(self) -> str:
        data = self._client().request("GET", "/whoami-v2")
        return f"Signed in as {data.get('name')} (type={data.get('type')})"

    def search_models(self, query: str = "", limit: str = "10") -> str:
        count = max(1, min(int(limit or 10), 50))
        params = {"limit": count, "sort": "downloads", "direction": "-1"}
        if (query or "").strip():
            params["search"] = query.strip()[:100]
        data = self._client().request("GET", "/models", params=params)
        return json_list(
            [f"{item.get('id')} (downloads={item.get('downloads', '?')})" for item in data]
        )

    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool("hf_whoami", "Show the Hugging Face account.", {}, self.whoami),
            ConnectorTool("hf_search_models", "Search models by name.",
                          {"query": "str (optional)", "limit": "int (optional, default 10)"},
                          self.search_models),
        ]

    def check(self) -> str:
        return self.whoami()
