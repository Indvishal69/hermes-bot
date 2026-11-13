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

import json
import logging
import re
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

# Models that emit chain-of-thought tokens. On Groq those tokens are billed
# against max_completion_tokens, so a long "thinking" run can leave the visible
# content empty with finish_reason=stop — the failure seen in production.
REASONING_MARKERS = ("gpt-oss", "qwen3", "deepseek-r1", "reasoning", "-r1")


def is_reasoning_model(name: str) -> bool:
    model = (name or "").lower()
    return any(marker in model for marker in REASONING_MARKERS)


# Model families that cannot answer a chat completion.
NON_CHAT_MARKERS = (
    "whisper",
    "tts",
    "orpheus",
    "guard",
    "embed",
    "rerank",
)


def salvage_failed_generation(raw: Any) -> str:
    """Recover usable output from Groq's ``tool_use_failed`` payload.

    gpt-oss models sometimes emit a native tool call even though no tools were
    sent ("Tool choice is none, but model called a tool"). Groq returns HTTP 400
    with ``failed_generation`` holding what the model actually produced — often
    the JSON object we asked for. Recovering it saves the step instead of
    failing the task.
    """
    text = raw if isinstance(raw, str) else str(raw or "")
    if not text.strip():
        return ""
    # Strip the model's tool-call markers, then look for a JSON object.
    cleaned = re.sub(r"<\|[^|]*\|>", " ", text)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return ""
    candidate = cleaned[start : end + 1]
    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return ""
    if isinstance(obj, dict) and isinstance(obj.get("action"), str):
        return json.dumps(obj)
    return ""


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


def _is_json(response: Any) -> bool:
    try:
        response.json()
        return True
    except Exception:  # noqa: BLE001 - malformed body is fine to ignore
        return False


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
        reasoning_effort: str = "low",
        secrets: tuple[str, ...] = (),
    ):
        self.api_key = api_key
        self.model = (model or "").strip()
        self.reasoning_effort = (reasoning_effort or "").strip().lower()
        self._reasoning_params_supported = True
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
    def _payload(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        *,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        system, turns = split_system(messages)
        payload_messages: list[dict[str, str]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(turns)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": temperature,
            # ``max_completion_tokens`` is accepted by every current chat model;
            # ``max_tokens`` is rejected by some of them.
            "max_completion_tokens": max_tokens,
        }
        effort = (reasoning_effort or self.reasoning_effort or "").strip().lower()
        if (
            self._reasoning_params_supported
            and effort in ("low", "medium", "high")
            and is_reasoning_model(self.model)
        ):
            # Keep thinking short so the visible JSON answer is not starved, and
            # hide the raw reasoning so it cannot confuse the action parser.
            payload["reasoning_effort"] = effort
            payload["reasoning_format"] = "hidden"
        return payload

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
        effort: str | None = None
        token_budget = max_tokens
        nudge_sent = False

        for attempt in range(1, self.max_retries + 1):
            payload = self._payload(
                messages, temperature, token_budget, reasoning_effort=effort
            )
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
                    try:
                        return self._parse(response)
                    except ProviderError as exc:
                        last_error = exc
                        if not exc.retryable:
                            raise
                        # Empty visible content: reasoning probably consumed the
                        # budget. Give the next attempt more room and less thinking.
                        token_budget = min(8_000, max(token_budget * 2, token_budget + 1_000))
                        effort = "low"
                        if attempt < self.max_retries:
                            log.warning(
                                "Groq returned empty content; retrying with "
                                "max_completion_tokens=%s, reasoning_effort=low",
                                token_budget,
                            )
                            continue
                        raise

                body = self._clean(response.text)
                kind = self._classify(response.status_code)
                lowered = body.lower()

                # A native tool call we never offered: salvage the JSON the
                # model produced, or retry once with an explicit instruction.
                if response.status_code == 400 and "tool_use_failed" in lowered:
                    salvaged = salvage_failed_generation(
                        (response.json().get("error") or {}).get("failed_generation")
                        if _is_json(response)
                        else ""
                    )
                    if salvaged:
                        log.warning("Groq tool_use_failed: recovered the JSON action")
                        return salvaged
                    if not nudge_sent:
                        nudge_sent = True
                        messages = list(messages) + [{
                            "role": "system",
                            "content": (
                                "Do not emit tool calls or function syntax. Reply with "
                                "exactly one JSON object and nothing else."
                            ),
                        }]
                        log.warning("Groq tool_use_failed: retrying with a JSON-only nudge")
                        continue
                    last_error = ProviderError(
                        self.name,
                        "the model tried to call a tool that was not offered "
                        "(tool_use_failed). Try a different GROQ_MODEL.",
                        kind=OTHER,
                        status=400,
                    )
                    raise last_error

                # Older/newer models may reject the reasoning knobs: drop them
                # once and retry immediately instead of failing the task.
                if response.status_code == 400 and self._reasoning_params_supported and (
                    "reasoning_effort" in lowered or "reasoning_format" in lowered
                ):
                    self._reasoning_params_supported = False
                    log.info("Groq rejected reasoning parameters; retrying without them")
                    continue
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
                    retry_after=parse_retry_after(body, response.headers),
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
            had_reasoning = False
            try:
                message = data["choices"][0]
                finish = message.get("finish_reason") or ""
                had_reasoning = bool((message.get("message") or {}).get("reasoning"))
            except (KeyError, IndexError, TypeError):
                pass
            detail = f"empty content (finish_reason={finish or 'unknown'})"
            if had_reasoning:
                detail += (
                    " — the model spent its output budget on reasoning tokens; "
                    "raise MAX_COMPLETION_TOKENS or set GROQ_MODEL to a "
                    "non-reasoning chat model"
                )
            raise ProviderError(
                self.name,
                detail,
                kind=OTHER,
                retryable=True,
            )
        return content.strip()

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": self.configured(),
            "model": self.model or "(auto)",
            "selection": self._resolved_reason,
            "reasoning_effort": self.reasoning_effort or "(default)",
            "models_error": self._models_error,
        }
