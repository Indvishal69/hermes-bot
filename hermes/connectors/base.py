"""Connector framework for Hermes.

A *connector* is a small, declarative bridge to an external service (GitHub,
Notion, Slack, …). The rules every connector follows:

* **Credentials come from environment variables only.** They are never written to
  the database, never shown in Discord, and are redacted from every error.
* **State is persisted.** Enabled/disabled flags and non-secret settings (a default
  repository, a database id) live in SQLite, so a connector stays connected across
  restarts and redeploys.
* **The model cannot choose a host, path or method.** Each tool builds its own URL
  from a fixed base; arguments are validated as safe identifiers.
* **Read tools run freely; write tools need the owner's `!approve`.** A tool
  declares ``write=True`` and the agent loop routes it through the approval gate.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

import requests

from ..utils import redact, truncate

log = logging.getLogger("hermes.connectors")

# Identifiers the model may pass (repo names, ids, paths). Deliberately narrow:
# no scheme, no host, no traversal, no whitespace.
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,99}$")
SAFE_PATH = re.compile(r"^[A-Za-z0-9._\-][A-Za-z0-9._/\-]{0,299}$")
SAFE_REPO = re.compile(r"^[A-Za-z0-9._\-]{1,100}/[A-Za-z0-9._\-]{1,100}$")


class ConnectorError(RuntimeError):
    """A connector failure with a message safe to show to the owner."""


def safe_id(value: str, *, what: str = "id") -> str:
    text = (value or "").strip()
    if not SAFE_ID.match(text):
        raise ConnectorError(
            f"Invalid {what}: only letters, digits, dot, dash and underscore are allowed."
        )
    return text


def safe_path(value: str, *, what: str = "path") -> str:
    text = (value or "").strip().lstrip("/")
    if not text or ".." in text or not SAFE_PATH.match(text):
        raise ConnectorError(f"Invalid {what}: use a relative path without '..'.")
    return text


def safe_repo(value: str, *, what: str = "repository") -> str:
    text = (value or "").strip()
    if not SAFE_REPO.match(text):
        raise ConnectorError(f"Invalid {what}: expected 'owner/name'.")
    return text


@dataclass
class ConnectorTool:
    """One agent-callable action exposed by a connector."""

    name: str
    description: str
    params: dict[str, str] = field(default_factory=dict)
    handler: Callable[..., str] | None = None
    write: bool = False
    approval_hint: str = ""

    def call(self, args: Mapping[str, Any]) -> str:
        if self.handler is None:
            raise ConnectorError(f"{self.name} has no handler")
        allowed = set(self.params)
        unknown = set(args) - allowed - {"action"}
        if unknown:
            raise ConnectorError(
                f"{self.name}: unknown argument(s) {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(allowed)) or 'none'}."
            )
        kwargs = {key: args[key] for key in allowed if key in args}
        return self.handler(**kwargs)

    def summary(self, args: Mapping[str, Any]) -> str:
        shown = ", ".join(
            f"{key}={truncate(str(args[key]), 60)}" for key in sorted(args) if key != "action"
        )
        return f"{self.name}({shown})"


class HttpClient:
    """Minimal JSON client with a fixed base URL, timeouts and redaction."""

    def __init__(
        self,
        name: str,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        *,
        timeout: int = 30,
        max_chars: int = 7_000,
        secrets: Iterable[str] = (),
        session: requests.Session | None = None,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.headers = {"Accept": "application/json", "User-Agent": "hermes-connectors/1.0"}
        if headers:
            self.headers.update(dict(headers))
        self.timeout = timeout
        self.max_chars = max_chars
        self.secrets = tuple(secrets)
        self._session = session or requests.Session()

    def _clean(self, value: Any) -> str:
        return redact(truncate(value, 900), self.secrets)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Mapping[str, Any] | None = None,
    ) -> Any:
        if not path.startswith("/"):
            raise ConnectorError(f"{self.name}: internal path must start with '/'.")
        if "://" in path or ".." in path:
            raise ConnectorError(f"{self.name}: refusing an unsafe path.")
        url = f"{self.base_url}{path}"
        try:
            response = self._session.request(
                method.upper(),
                url,
                headers=self.headers,
                params=dict(params) if params else None,
                json=json_body,
                data=dict(data) if data else None,
                timeout=(10, self.timeout),
            )
        except requests.RequestException as exc:
            raise ConnectorError(
                f"{self.name}: network error {type(exc).__name__}: {self._clean(exc)}"
            ) from exc

        if response.status_code >= 400:
            body = self._clean(response.text)
            hint = ""
            if response.status_code in (401, 403):
                hint = " — check that the token is valid and has the required scope"
            elif response.status_code == 404:
                hint = " — check the id/path"
            elif response.status_code == 429:
                hint = " — rate limited, try again later"
            raise ConnectorError(
                f"{self.name}: HTTP {response.status_code}{hint}: {body}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": self._clean(response.text)}


class Connector:
    """Base class: declare metadata, tools and a cheap ``check`` call."""

    name: str = "connector"
    title: str = "Connector"
    description: str = ""
    env_vars: tuple[str, ...] = ()
    optional_env_vars: tuple[str, ...] = ()
    docs_url: str = ""
    settings_schema: dict[str, str] = {}

    def __init__(
        self,
        credentials: Mapping[str, str] | None = None,
        settings: Mapping[str, str] | None = None,
        *,
        timeout: int = 30,
        max_chars: int = 7_000,
        secrets: Iterable[str] = (),
        session: requests.Session | None = None,
        workspace: Any = None,
    ):
        self.credentials = dict(credentials or {})
        # Only connectors that read files from the sandbox need this.
        self.workspace = workspace
        self.settings = {key: str(value) for key, value in (settings or {}).items()}
        self.timeout = timeout
        self.max_chars = max_chars
        self.secrets = tuple(secrets)
        self.session = session

    # -- credentials / settings --------------------------------------------
    @property
    def missing_env(self) -> list[str]:
        return [key for key in self.env_vars if not (self.credentials.get(key) or "").strip()]

    @property
    def configured(self) -> bool:
        return not self.missing_env

    def credential(self, key: str) -> str:
        value = (self.credentials.get(key) or "").strip()
        if not value:
            raise ConnectorError(f"{self.name}: {key} is not configured")
        return value

    def setting(self, key: str, default: str = "") -> str:
        value = (self.settings.get(key) or "").strip()
        return value or default

    def require_setting(self, key: str, label: str) -> str:
        value = self.setting(key)
        if not value:
            raise ConnectorError(
                f"{self.name}: no {label} given and no default saved. "
                f"Set one with: !connector {self.name} set {key}=<value>"
            )
        return value

    # -- tools --------------------------------------------------------------
    def tools(self) -> list[ConnectorTool]:
        return []

    def tool(self, name: str) -> ConnectorTool | None:
        return next((tool for tool in self.tools() if tool.name == name), None)

    def check(self) -> str:
        """Cheap live check that the credentials work (shown by ``!connector X test``)."""
        raise ConnectorError(f"{self.name}: this connector has no live check")

    # -- reporting ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "configured": self.configured,
            "missing_env": self.missing_env,
            "env_vars": list(self.env_vars),
            "optional_env_vars": list(self.optional_env_vars),
            "settings": dict(self.settings),
            "settings_schema": dict(self.settings_schema),
            "tools": [
                {"name": tool.name, "write": tool.write, "description": tool.description}
                for tool in self.tools()
            ],
            "docs_url": self.docs_url,
        }

    def client(self, base_url: str, headers: Mapping[str, str] | None = None) -> HttpClient:
        return HttpClient(
            self.name,
            base_url,
            headers,
            timeout=self.timeout,
            max_chars=self.max_chars,
            secrets=self.secrets,
            session=self.session,
        )


def json_list(items: list[str], *, empty: str = "(none)") -> str:
    return "\n".join(items) if items else empty
