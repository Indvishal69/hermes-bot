"""Connector framework: pluggable, persisted integrations for the agent."""

from .base import (
    Connector,
    ConnectorError,
    ConnectorTool,
    HttpClient,
    safe_id,
    safe_path,
    safe_repo,
)
from .github import GitHubConnector
from .netlify import NetlifyConnector
from .notion import NotionConnector
from .platforms import (
    CloudflareConnector,
    HuggingFaceConnector,
    RenderConnector,
    VercelConnector,
)
from .registry import CONNECTOR_CLASSES, ConnectorRegistry
from .slack import SlackConnector

__all__ = [
    "Connector",
    "ConnectorError",
    "ConnectorTool",
    "HttpClient",
    "safe_id",
    "safe_path",
    "safe_repo",
    "GitHubConnector",
    "NotionConnector",
    "SlackConnector",
    "NetlifyConnector",
    "RenderConnector",
    "VercelConnector",
    "CloudflareConnector",
    "HuggingFaceConnector",
    "ConnectorRegistry",
    "CONNECTOR_CLASSES",
]
