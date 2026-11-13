"""OpenRouter provider with automatic model switching.

OpenRouter is OpenAI-compatible but fronts hundreds of models with different
prices, contexts and availability. Two things matter for an agent:

* **`openrouter/auto`** lets OpenRouter pick a model per request — a sane default.
* **Model rotation.** A model can be retired, gated, or rate-limited on its own.
  When one fails for a *model-level* reason (404/400/403/429), the next model in
  ``OPENROUTER_FALLBACK_MODELS`` is tried before the whole provider is given up on.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import requests

from ..utils import backoff_delay, parse_retry_after, redact, truncate
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

log = logging.getLogger("hermes.providers.openrouter")

API_BASE = "https://openrouter.ai/api/v1"
AUTO_MODEL = "openrouter/auto"


def build_model_chain(primary: str, fallbacks: Iterable[str]) -> list[str]:
    """Primary first, then fallbacks, de-duplicated and without blanks."""
    chain: list[str] = []
    for name in [primary or AUTO_MODEL, *fallbacks]:
        candidate = (name or "").strip()
        if candidate and candidate not in chain:
            chain.append(candidate)
    return chain or [AUTO_MODEL]


class OpenRouterProvider(Provider):
    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str = AUTO_MODEL,
        *,
        fallbacks: Iterable[str] = (),
        session: requests.Session | None = None,
        max_retries: int = 2,
        model_cache_ttl: int = 3_600,
        referer: str = "https://github.com/Indvishal69/hermes-bot",
        secrets: tuple[str, ...] = (),
    ):
        self.api_key = api_key
        self.model = (model or AUTO_MODEL).strip()
        self.chain = build_model_chain(self.model, fallbacks)
        self.max_retries = max_retries
        self.model_cache_ttl = model_cache_ttl
        self.referer = referer
        self._session = session or requests.Session()
        self.secrets = secrets
        self._models: list[str] = []
        self._models_ts = 0.0
        self._models_error = ""
        self._last_used = ""

    # -- plumbing -----------------------------------------------------------
    def configured(self) -> bool:
        return bool(self.api_key)

    def _clean(self, value: Any) -> str:
        return redact(truncate(value, 1_200), self.secrets)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.referer,
            "X-Title": "Hermes",
            "User-Agent": "hermes-3.3",
        }

    @staticmethod
    def _classify(status: int) -> str:
        if status in (401,):
            return AUTH
        if status == 403:
            return MODEL          # model gated for this account
        if status == 404:
            return MODEL
        if status == 429:
            return QUOTA
        if status >= 500:
            return NETWORK
        return OTHER

    # -- models -------------------------------------------------------------
    def list_models(self, *, refresh: bool = False) -> list[str]:
        if not self.configured():
            return []
        if not refresh and self._models and time.time() - self._models_ts < self.model_cache_ttl:
            return list(self._models)
        try:
            response = self._session.get(
                f"{API_BASE}/models", headers=self._headers(), timeout=(10, 30)
            )
            if response.status_code != 200:
                self._models_error = self._clean(
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )
                return list(self._models)
            payload = response.json()
            ids = sorted(
                item.get("id", "")
                for item in payload.get("data", [])
                if isinstance(item, dict) and item.get("id")
            )
            self._models = [name for name in ids if name]
            self._models_ts = time.time()
            self._models_error = ""
        except (requests.RequestException, ValueError) as exc:
            self._models_error = self._clean(f"{type(exc).__name__}: {exc}")
            log.warning("OpenRouter model listing failed: %s", self._models_error)
        return list(self._models)

    def models_error(self) -> str:
        return self._models_error

    def current_model(self) -> str:
        return self._last_used or self.model

    def selection_reason(self) -> str:
        if self._last_used and self._last_used != self.model:
            return f"rotated to {self._last_used} after {self.model} failed"
        return f"using {self.model}"

    def resolve_model(self, *, refresh: bool = False) -> str:
        """Keep the primary unless the API says it does not exist."""
        available = self.list_models(refresh=refresh)
        if not available or self.model == AUTO_MODEL or self.model in available:
            return self.model
        alternative = next((name for name in self.chain if name in available), None)
        if alternative:
            log.warning("OpenRouter model %s unavailable; using %s", self.model, alternative)
            self.chain = build_model_chain(alternative, self.chain)
            self.model = alternative
        return self.model

    # -- chat ---------------------------------------------------------------
    def _payload(self, messages: list[dict[str, str]], temperature: float, max_tokens: int, model: str) -> dict[str, Any]:
        system, turns = split_system(messages)
        payload_messages: list[dict[str, str]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(turns)
        return {
            "model": model,
            "messages": payload_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
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
            raise ProviderError(self.name, "OPENROUTER_API_KEY is not configured", kind=AUTH)

        per_attempt = max(10, min(int(timeout), 50))
        models = list(self.chain)
        last_error: ProviderError | None = None

        for index, model in enumerate(models):
            more_models = index < len(models) - 1
            for attempt in range(1, self.max_retries + 1):
                try:
                    response = self._session.post(
                        f"{API_BASE}/chat/completions",
                        headers=self._headers(),
                        json=self._payload(messages, temperature, max_tokens, model),
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
                        text = self._parse(response)
                        self._last_used = model
                        return text

                    body = self._clean(response.text)
                    kind = self._classify(response.status_code)
                    lowered = body.lower()
                    if "model" in lowered and ("not found" in lowered or "no endpoints" in lowered):
                        kind = MODEL
                    last_error = ProviderError(
                        self.name,
                        f"{model}: HTTP {response.status_code}: {body}",
                        kind=kind,
                        status=response.status_code,
                        retry_after=parse_retry_after(body, response.headers),
                    )
                    # A model-level problem: switch models instead of retrying it.
                    # An auth error is about the key, so rotating cannot help.
                    if kind in (MODEL, QUOTA) and more_models:
                        log.warning(
                            "OpenRouter model %s failed (%s); trying %s",
                            model, kind, models[index + 1],
                        )
                        break
                    if not last_error.retryable:
                        raise last_error

                if attempt < self.max_retries:
                    delay = backoff_delay(attempt)
                    log.warning(
                        "OpenRouter %s attempt %s/%s failed; retrying in %ss",
                        model, attempt, self.max_retries, delay,
                    )
                    time.sleep(delay)
            else:
                continue

        raise last_error or ProviderError(self.name, "all models failed", kind=OTHER)

    def _parse(self, response: requests.Response) -> str:
        try:
            data = response.json()
            choice = data["choices"][0]
            content = (choice.get("message") or {}).get("content")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                self.name,
                f"unexpected response shape: {self._clean(response.text[:300])}",
                kind=OTHER,
            ) from exc
        if isinstance(content, list):  # some models return content parts
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            finish = ""
            try:
                finish = data["choices"][0].get("finish_reason") or ""
            except (KeyError, IndexError, TypeError):
                pass
            raise ProviderError(
                self.name,
                f"empty content (finish_reason={finish or 'unknown'})",
                kind=OTHER,
                retryable=True,
            )
        return content.strip()

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": self.configured(),
            "model": self.current_model(),
            "chain": list(self.chain),
            "selection": self.selection_reason(),
            "models_error": self._models_error,
        }
