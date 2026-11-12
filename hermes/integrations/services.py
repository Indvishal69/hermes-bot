"""Render, Vercel, Cloudflare and Hugging Face integrations (prepared, opt-in)."""

from __future__ import annotations

from typing import Any

from .base import Integration


class RenderIntegration(Integration):
    name = "render"
    env_var = "RENDER_API_KEY"
    base_url = "https://api.render.com/v1"

    def list_services(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/services", params={"limit": 50})
        return [item.get("service", item) for item in data if isinstance(item, dict)]

    def trigger_deploy(self, service_id: str, *, clear_cache: bool = False) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/services/{service_id}/deploys",
            json_body={"clearCache": "clear" if clear_cache else "do_not_clear"},
        )

    def deploys(self, service_id: str) -> list[dict[str, Any]]:
        data = self.request("GET", f"/services/{service_id}/deploys", params={"limit": 10})
        return [item.get("deploy", item) for item in data if isinstance(item, dict)]


class VercelIntegration(Integration):
    name = "vercel"
    env_var = "VERCEL_TOKEN"
    base_url = "https://api.vercel.com"

    def list_projects(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/v9/projects", params={"limit": 20})
        return data.get("projects", [])

    def list_deployments(self, limit: int = 10) -> list[dict[str, Any]]:
        data = self.request("GET", "/v6/deployments", params={"limit": limit})
        return data.get("deployments", [])


class CloudflareIntegration(Integration):
    name = "cloudflare"
    env_var = "CLOUDFLARE_API_TOKEN"
    base_url = "https://api.cloudflare.com/client/v4"

    def verify(self) -> dict[str, Any]:
        return self.request("GET", "/user/tokens/verify")

    def purge_cache(self, zone_id: str, urls: list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"purge_everything": True} if not urls else {"files": urls}
        return self.request("POST", f"/zones/{zone_id}/purge_cache", json_body=body)


class HuggingFaceIntegration(Integration):
    name = "huggingface"
    env_var = "HF_TOKEN"
    base_url = "https://huggingface.co/api"

    def whoami(self) -> dict[str, Any]:
        return self.request("GET", "/whoami-v2")

    def list_models(self, search: str = "", limit: int = 10) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if search:
            params["search"] = search
        return self.request("GET", "/models", params=params)
