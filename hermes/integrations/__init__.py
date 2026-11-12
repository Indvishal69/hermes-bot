"""Optional integrations, constructed from environment variables only."""

from __future__ import annotations

from .base import Integration, IntegrationError
from .github import GitHubIntegration
from .services import (
    CloudflareIntegration,
    HuggingFaceIntegration,
    RenderIntegration,
    VercelIntegration,
)

__all__ = [
    "Integration",
    "IntegrationError",
    "GitHubIntegration",
    "RenderIntegration",
    "VercelIntegration",
    "CloudflareIntegration",
    "HuggingFaceIntegration",
    "build_integrations",
]


def build_integrations(settings) -> dict[str, Integration]:
    """Return every integration with its credentials attached (may be unconfigured)."""
    secrets = settings.secrets
    return {
        "github": GitHubIntegration(settings.github_token, secrets=secrets),
        "render": RenderIntegration(settings.render_api_key, secrets=secrets),
        "vercel": VercelIntegration(settings.vercel_token, secrets=secrets),
        "cloudflare": CloudflareIntegration(settings.cloudflare_api_token, secrets=secrets),
        "huggingface": HuggingFaceIntegration(settings.huggingface_token, secrets=secrets),
    }
