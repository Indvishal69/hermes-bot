"""Shared HTTP plumbing for optional integrations.

Integrations are *prepared*, never automatic: nothing here runs unless a command
or the owner explicitly calls it. Tokens come only from environment variables and
are redacted from every error message.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from ..utils import redact, truncate

log = logging.getLogger("hermes.integrations")


class IntegrationError(RuntimeError):
    pass


class Integration:
    name = "integration"
    env_var = ""
    base_url = ""

    def __init__(self, token: str = "", *, timeout: int = 30, secrets: tuple[str, ...] = ()):
        self.token = token or ""
        self.timeout = timeout
        self._secrets = tuple(secrets) + ((self.token,) if self.token else ())
        self._session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "hermes-3.0",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self.configured:
            raise IntegrationError(f"{self.name}: {self.env_var} is not set")
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        try:
            response = self._session.request(
                method,
                url,
                headers=self.headers(),
                json=json_body,
                params=params,
                timeout=(10, self.timeout),
            )
        except requests.RequestException as exc:
            raise IntegrationError(
                f"{self.name}: network error {type(exc).__name__}: "
                f"{redact(exc, self._secrets)}"
            ) from exc

        if response.status_code >= 400:
            body = redact(truncate(response.text, 800), self._secrets)
            raise IntegrationError(f"{self.name}: HTTP {response.status_code} — {body}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": truncate(response.text, 800)}

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "env_var": self.env_var, "configured": self.configured}
