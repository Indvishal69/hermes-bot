"""Groq provider (OpenAI-compatible chat completions).

Uses ``requests`` directly so retries, error classification and **model
discovery** are explicit.

Model discovery matters as much as it does for Gemini: Groq retires model IDs on
a published schedule (``llama-3.3-70b-versatile`` and ``llama-3.1-8b-instant``
were shut down for free/developer tiers on 16 August 2026, replaced by
``openai/gpt-oss-120b``). A hardcoded default therefore stops working without
warning, so the configured model is checked against
``GET /openai/v1/models`` and a working chat model is chosen when it is gone.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

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
    choose_model,
    split_system,
)

log = logging.getLogger("hermes.providers.groq")

API_BASE = "https://api.groq.com/openai/v1"

# Preference order when the configured model is unavailable.
PREFERRED_CHAT_MODELS = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "qwen/qwen3.6-27b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
)

# Model families that cannot answer a chat completion.
NON_CHAT_MARKERS = (
    "whisper",
    "tts",
    "orpheus",
    "guard",
    "embed",
    "rerank",
)


def is_chat_model(name: str) -> bool:
    """True when *name* looks like a Groq chat/completion model."""
    model = (name or "").strip().lower()
    if not model:
        return False
    return not any(marker in model for marker in NON_CHAT_MARKERS)


def select_model(available: Iterable[str], configured: str = "") -> tuple[str | None, str]:
    """Choose a usable Groq chat model. Pure function, unit tested."""
    return choose_model(
        available,
        configured,
        preferred=PREFERRED_CHAT_MODELS,
        usable=is_chat_model,
        label="chat model",
    )


class GroqProvider(Provider):
    name = "groq"

    def __init__(
        self,
        api_key: str,
        model: str = "openai/gpt-oss-120b",
        *,
        session: requests.Session | None = None,
        max_retries: int = 3,
        model_cache_ttl: int = 3600,
        secrets: tuple[str, ...] = (),
    ):
        self.api_key = api_key
        self.model = (model or "").strip()
        self.max_retries = max_retries
        self.model_cache_ttl = model_cache_ttl
        self._session = session or requests.Session()
        self._secrets = secrets
        self._models_cache: list[str] = []
        self._models_ts = 0.0
        self._models_error: str = ""
        self._resolved_reason: str = ""

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
        return OTHER

    # -- models -------------------------------------------------------------
    def list_models(self, *, refresh: bool = False) -> list[str]:
        if not self.configured():
            return []
        if (
            not refresh
            and self._models_cache
            and time.time() - self._models_ts < self.model_cache_ttl
        ):
            return list(self._models_cache)
        try:
            response = self._session.get(
                f"{API_BASE}/models", headers=self._headers(), timeout=(10, 30)
            )
            if response.status_code != 200:
                self._models_error = self._clean(
                    f"HTTP {response.status_code}: {response.text[:400]}"
                )
                log.warning("Groq model listing failed: %s", self._models_error)
                return list(self._models_cache)
            payload = response.json()
            ids = sorted(
                item.get("id", "")
                for item in payload.get("data", [])
                if isinstance(item, dict) and item.get("id")
            )
            self._models_cache = [name for name in ids if name]
            self._models_ts = time.time()
            self._models_error = "" if self._models_cache else "API returned no models"
        except (requests.RequestException, ValueError) as exc:
            self._models_error = self._clean(f"{type(exc).__name__}: {exc}")
            log.warning("Groq model listing failed: %s", self._models_error)
        return list(self._models_cache)

    def models_error(self) -> str:
        return self._models_error

    def selection_reason(self) -> str:
        return self._resolved_reason

    def resolve_model(self, *, refresh: bool = False) -> str:
        """Pick a chat model that this API key can actually use."""
        models = self.list_models(refresh=refresh)
        chosen, reason = select_model(models, self.model)
        self._resolved_reason = reason
        if not chosen:
            raise ProviderError(
                self.name,
                f"no usable chat model ({reason}). {self._models_error}".strip(),
                kind=MODEL,
            )
        if chosen != self.model:
            log.warning("Groq model selection: %s", reason)
        self.model = chosen
        return chosen

    # -- chat ---------------------------------------------------------------
    def _payload(self, messages: list[dict[str, str]], temperature: float, max_tokens: int) -> dict[str, Any]:
        system, turns = split_system(messages)
        payload_messages: list[dict[str, str]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(turns)
        return {
            "model": self.model,
            "messages": payload_messages,
            "temperature": temperature,
            # ``max_completion_tokens`` is accepted by every current chat model;
            # ``max_tokens`` is rejected by some of them.
            "max_completion_tokens": max_tokens,
        }

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
        if not system and not turns:
            raise ProviderError(self.name, "No messages to send", kind=OTHER)

        model = self.model or self.resolve_model()
        # Bound every attempt so the retry loop cannot outlive the caller's
        # AI_TIMEOUT.
        per_attempt = max(10, min(int(timeout), 50))
        resolved_once = False
        last_error: ProviderError | None = None

        for attempt in range(1, self.max_retries + 1):
            payload = self._payload(messages, temperature, max_tokens)
            payload["model"] = model
            try:
                response = self._session.post(
                    f"{API_BASE}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=(10, per_attempt),
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
                lowered = body.lower()
                if response.status_code == 404 or (
                    response.status_code == 400
                    and ("model_not_found" in lowered or "does not exist" in lowered)
                ):
                    kind = MODEL
                last_error = ProviderError(
                    self.name,
                    f"HTTP {response.status_code}: {body}",
                    kind=kind,
                    status=response.status_code,
                )
                if kind == MODEL and not resolved_once:
                    # The ID was retired or is not on this plan: re-discover once.
                    resolved_once = True
                    try:
                        model = self.resolve_model(refresh=True)
                    except ProviderError:
                        raise last_error from None
                    log.warning("Groq model unavailable; retrying with %s", model)
                    continue
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
            "model": self.model or "(auto)",
            "selection": self._resolved_reason,
            "models_error": self._models_error,
        }
