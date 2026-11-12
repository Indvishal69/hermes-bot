"""Groq provider (OpenAI-compatible chat completions).

Uses ``requests`` directly so retries, error classification and model
discovery are explicit rather than hidden behind a client.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from ..utils import backoff_delay, redact, truncate
from .base import (
    AUTH,
    MODEL,
    NETWORK,
    OTHER,
    QUOTA,
    Provider,
    ProviderError,
    split_system,
)

log = logging.getLogger("hermes.providers.groq")

API_BASE = "https://api.groq.com/openai/v1"


class GroqProvider(Provider):
    name = "groq"

    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        *,
        session: requests.Session | None = None,
        max_retries: int = 3,
        secrets: tuple[str, ...] = (),
    ):
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self._session = session or requests.Session()
        self._secrets = secrets
        self._models_cache: list[str] = []
        self._models_ts = 0.0
        self._models_error: str = ""

    # -- helpers ------------------------------------------------------------
    def configured(self) -> bool:
        return bool(self.api_key)

    def _clean(self, text: Any) -> str:
        return redact(truncate(text, 1200), self._secrets)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "hermes-3.0",
        }

    @staticmethod
    def _classify(status: int) -> str:
        if status in (401, 403):
            return AUTH
        if status == 429:
            return QUOTA
        if status == 404:
            return MODEL
        if status >= 500:
            return NETWORK
        if status == 400:
            return OTHER
        return OTHER

    # -- models -------------------------------------------------------------
    def list_models(self, *, refresh: bool = False) -> list[str]:
        if not self.configured():
            return []
        if not refresh and self._models_cache and time.time() - self._models_ts < 3600:
            return list(self._models_cache)
        try:
            response = self._session.get(
                f"{API_BASE}/models", headers=self._headers(), timeout=(10, 30)
            )
            if response.status_code != 200:
                self._models_error = self._clean(
                    f"HTTP {response.status_code}: {response.text[:400]}"
                )
                return list(self._models_cache)
            payload = response.json()
            models = sorted(
                item.get("id", "")
                for item in payload.get("data", [])
                if isinstance(item, dict) and item.get("id")
            )
            self._models_cache = [m for m in models if m]
            self._models_ts = time.time()
            self._models_error = ""
        except (requests.RequestException, ValueError) as exc:
            self._models_error = self._clean(f"{type(exc).__name__}: {exc}")
            log.warning("Groq model listing failed: %s", self._models_error)
        return list(self._models_cache)

    def models_error(self) -> str:
        return self._models_error

    # -- chat ---------------------------------------------------------------
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 3000,
        timeout: int = 120,
    ) -> str:
        if not self.configured():
            raise ProviderError(self.name, "GROQ_API_KEY is not configured", kind=AUTH)

        system, turns = split_system(messages)
        payload_messages: list[dict[str, str]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(turns)
        if not payload_messages:
            raise ProviderError(self.name, "No messages to send", kind=OTHER)

        payload = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        last_error: ProviderError | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._session.post(
                    f"{API_BASE}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=(10, timeout),
                )
            except requests.RequestException as exc:
                last_error = ProviderError(
                    self.name,
                    f"network error {type(exc).__name__}: {self._clean(exc)}",
                    kind=NETWORK,
                )
            else:
                if response.status_code == 200:
                    return self._parse(response)
                body = self._clean(response.text)
                kind = self._classify(response.status_code)
                last_error = ProviderError(
                    self.name,
                    f"HTTP {response.status_code}: {body}",
                    kind=kind,
                    status=response.status_code,
                )
                if response.status_code == 400 and "model" in body.lower():
                    last_error = ProviderError(
                        self.name,
                        f"model '{self.model}' rejected: {body}",
                        kind=MODEL,
                        status=400,
                    )
                if not last_error.retryable:
                    raise last_error
            if attempt < self.max_retries:
                delay = backoff_delay(attempt)
                log.warning(
                    "Groq attempt %s/%s failed (%s); retrying in %ss",
                    attempt,
                    self.max_retries,
                    last_error.detail if last_error else "unknown",
                    delay,
                )
                time.sleep(delay)
        raise last_error or ProviderError(self.name, "unknown failure", kind=OTHER)

    def _parse(self, response: requests.Response) -> str:
        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                self.name,
                f"unexpected response shape: {self._clean(response.text[:400])}",
                kind=OTHER,
            ) from exc
        if not isinstance(content, str) or not content.strip():
            finish = ""
            try:
                finish = data["choices"][0].get("finish_reason") or ""
            except (KeyError, IndexError, TypeError):
                pass
            raise ProviderError(
                self.name,
                f"empty message (finish_reason={finish or 'unknown'})",
                kind=OTHER,
            )
        return content.strip()

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": self.configured(),
            "model": self.model,
            "models_error": self._models_error,
        }
