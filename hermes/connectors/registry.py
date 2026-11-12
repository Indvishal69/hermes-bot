"""Connector registry: persistence, enable/disable, tool exposure.

Enabled/disabled state and non-secret settings are stored in SQLite, so a
connector that was connected once stays connected after a restart or redeploy
(as long as ``DATA_DIR`` itself survives — see docs/DEPLOYMENT.md).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Mapping

from ..db import Database
from ..utils import redact, truncate
from .base import Connector, ConnectorError, ConnectorTool
from .github import GitHubConnector
from .netlify import NetlifyConnector
from .notion import NotionConnector
from .platforms import (
    CloudflareConnector,
    HuggingFaceConnector,
    RenderConnector,
    VercelConnector,
)
from .slack import SlackConnector

log = logging.getLogger("hermes.connectors.registry")

CONNECTOR_CLASSES: tuple[type[Connector], ...] = (
    GitHubConnector,
    NotionConnector,
    SlackConnector,
    NetlifyConnector,
    RenderConnector,
    VercelConnector,
    CloudflareConnector,
    HuggingFaceConnector,
)


class ConnectorRegistry:
    """Owns the connectors, their saved state, and the tools they expose."""

    def __init__(
        self,
        db: Database,
        *,
        env: Mapping[str, str] | None = None,
        secrets: Iterable[str] = (),
        timeout: int = 30,
        max_chars: int = 7_000,
        classes: Iterable[type[Connector]] = CONNECTOR_CLASSES,
    ):
        import os

        self.db = db
        self.env = dict(env if env is not None else os.environ)
        self.secrets = tuple(secrets)
        self.timeout = timeout
        self.max_chars = max_chars
        self.classes = tuple(classes)
        self.connectors: dict[str, Connector] = {}
        self.refresh()

    # -- construction -------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild connector instances from the environment + saved state."""
        saved = self.db.all_connectors()
        built: dict[str, Connector] = {}
        for cls in self.classes:
            state = saved.get(cls.name, {})
            credentials = {
                key: (self.env.get(key) or "").strip()
                for key in (*cls.env_vars, *cls.optional_env_vars)
            }
            built[cls.name] = cls(
                credentials,
                state.get("settings", {}),
                timeout=self.timeout,
                max_chars=self.max_chars,
                secrets=self.secrets,
            )
        self.connectors = built

    # -- state --------------------------------------------------------------
    def names(self) -> list[str]:
        return list(self.connectors)

    def get(self, name: str) -> Connector | None:
        return self.connectors.get((name or "").strip().lower())

    def _row(self, name: str) -> dict[str, Any]:
        return self.db.connector_state(name) or {}

    def is_disabled(self, name: str) -> bool:
        row = self._row(name)
        return row.get("enabled") is False

    def is_active(self, name: str) -> bool:
        connector = self.get(name)
        if connector is None or not connector.configured:
            return False
        return not self.is_disabled(name)

    def enable(self, name: str) -> str:
        connector = self.get(name)
        if connector is None:
            raise ConnectorError(f"Unknown connector '{name}'.")
        if not connector.configured:
            missing = ", ".join(connector.missing_env)
            raise ConnectorError(
                f"{connector.title} needs {missing} in the environment first. "
                "Add it on the host (Render → Environment), then redeploy."
            )
        self.db.save_connector(name, enabled=True)
        log.info("Connector enabled: %s", name)
        return f"{connector.title} connected."

    def disable(self, name: str) -> str:
        connector = self.get(name)
        if connector is None:
            raise ConnectorError(f"Unknown connector '{name}'.")
        self.db.save_connector(name, enabled=False)
        log.info("Connector disabled: %s", name)
        return f"{connector.title} disconnected. Its tools are no longer offered to the agent."

    def set_setting(self, name: str, key: str, value: str) -> str:
        connector = self.get(name)
        if connector is None:
            raise ConnectorError(f"Unknown connector '{name}'.")
        key = (key or "").strip()
        if key not in connector.settings_schema:
            allowed = ", ".join(connector.settings_schema) or "none"
            raise ConnectorError(f"{connector.title} has no setting '{key}'. Allowed: {allowed}.")
        value = (value or "").strip()
        if len(value) > 300:
            raise ConnectorError("Setting value is too long (max 300 characters).")
        settings = dict(connector.settings)
        if value:
            settings[key] = value
        else:
            settings.pop(key, None)
        self.db.save_connector(name, settings=settings)
        self.refresh()
        return f"{connector.title}: {key} = {value or '(cleared)'}"

    # -- tools --------------------------------------------------------------
    def active_tools(self) -> dict[str, tuple[Connector, ConnectorTool]]:
        tools: dict[str, tuple[Connector, ConnectorTool]] = {}
        for name, connector in self.connectors.items():
            if not self.is_active(name):
                continue
            for tool in connector.tools():
                if tool.name in tools:
                    raise ConnectorError(f"Duplicate connector tool name: {tool.name}")
                tools[tool.name] = (connector, tool)
        return tools

    def tool_specs(self) -> dict[str, dict[str, Any]]:
        """Prompt-shaped specs for every tool of every active connector."""
        specs: dict[str, dict[str, Any]] = {}
        for name, (connector, tool) in self.active_tools().items():
            args = dict(tool.params)
            specs[name] = {
                "args": args,
                "desc": f"[{connector.title}] {tool.description}"
                + (" — needs owner approval" if tool.write else ""),
                "approval": tool.write,
            }
        return specs

    def call(self, tool_name: str, args: Mapping[str, Any] | None = None) -> str:
        """Execute a connector tool, enforcing enabled/configured state."""
        entry = self.active_tools().get(tool_name)
        if entry is None:
            raise ConnectorError(
                f"Connector tool '{tool_name}' is not available. "
                "Check !connectors to see what is connected."
            )
        connector, tool = entry
        try:
            result = tool.call(dict(args or {}))
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw traceback
            raise ConnectorError(
                f"{tool_name} failed: {type(exc).__name__}: "
                f"{redact(str(exc), self.secrets)}"
            ) from exc
        return truncate(result, self.max_chars)

    def needs_approval(self, tool_name: str) -> bool:
        entry = self.active_tools().get(tool_name)
        return bool(entry and entry[1].write)

    def summary(self, tool_name: str, args: Mapping[str, Any]) -> str:
        entry = self.active_tools().get(tool_name)
        if entry is None:
            return tool_name
        _, tool = entry
        return tool.summary(dict(args or {}))

    # -- reporting ----------------------------------------------------------
    def status(self) -> list[dict[str, Any]]:
        rows = []
        for name, connector in self.connectors.items():
            row = connector.status()
            row["enabled"] = self.is_active(name)
            row["disabled_by_owner"] = self.is_disabled(name)
            row["tool_count"] = len(connector.tools())
            rows.append(row)
        return rows

    def active_count(self) -> int:
        return sum(1 for name in self.connectors if self.is_active(name))
