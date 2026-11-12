"""Gemini provider built on Google's official ``google-genai`` SDK.

Why this module exists (the original Gemini failure): the old code pinned
``google-genai<2``, hardcoded a model ID, flattened the whole conversation into
one string, and called ``response.text`` on possibly-blocked responses. Here:

* the model is *discovered* from the API key (``client.models.list()``),
* conversation turns keep their role structure,
* system instructions are passed as ``system_instruction``,
* quota / auth / model / network errors are classified and reported verbatim,
* retryable failures use exponential backoff, and
* blocked or empty candidates report the real finish reason.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

from ..utils import backoff_delay, parse_retry_after, redact, truncate
from .base import (
    AUTH,
    BLOCKED,
    MODEL,
    NETWORK,
    OTHER,
    QUOTA,
    Provider,
    ProviderError,
    choose_model,
    split_system,
)

log = logging.getLogger("hermes.providers.gemini")

# Model families that cannot answer a text chat request.
NON_TEXT_MARKERS = (
    "embedding",
    "embed",
    "tts",
    "live",
    "image",
    "nano-banana",
    "imagen",
    "veo",
    "transcribe",
    "computer-use",
    "deep-research",
    "native-audio",
)

# Order of preference when the configured model is unavailable.
PREFERRED_TEXT_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
)


def normalise_model_name(name: str) -> str:
    """``models/gemini-3.8-flash`` -> ``gemini-3.8-flash``."""
    value = (name or "").strip()
    if value.startswith("models/"):
        value = value.split("/", 1)[1]
    return value


def is_text_model(name: str, actions: Iterable[str] | None = None) -> bool:
    """True when *name* is a plain text-generation model."""
    model = normalise_model_name(name).lower()
    if not model:
        return False
    if any(marker in model for marker in NON_TEXT_MARKERS):
        return False
    if actions is not None:
        action_names = {str(a).lower() for a in actions}
        if action_names and not any("generatecontent" in a for a in action_names):
            return False
    return True


def select_model(
    available: Iterable[str], configured: str = ""
) -> tuple[str | None, str]:
    """Choose a usable text model. Pure function so it can be unit tested.

    Returns ``(model, reason)``; ``model`` is ``None`` only when *available*
    is empty and nothing was configured.
    """
    names = [normalise_model_name(name) for name in available if normalise_model_name(name)]
    return choose_model(
        names,
        normalise_model_name(configured),
        preferred=PREFERRED_TEXT_MODELS,
        usable=is_text_model,
        label="text-generation model",
        # Prefer a "flash" tier when nothing in the preferred list is listed:
        # cheapest reliable option for an agent loop.
        fallback_key=lambda name: (0 if "flash" in name.lower() else 1, name),
    )


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "",
        *,
        api_base: str = "",
        max_retries: int = 3,
        model_cache_ttl: int = 3600,
        client: Any | None = None,
        secrets: tuple[str, ...] = (),
    ):
        self.api_key = api_key
        self.model = normalise_model_name(model)
        self.api_base = api_base
        self.max_retries = max_retries
        self.model_cache_ttl = model_cache_ttl
        self._client = client
        self._secrets = secrets
        self._models: list[str] = []
        self._models_ts = 0.0
        self._models_error = ""
        self._resolved_reason = ""
        self._genai_import_error = ""

    # -- plumbing -----------------------------------------------------------
    def configured(self) -> bool:
        return bool(self.api_key)

    def _clean(self, text: Any) -> str:
        return redact(truncate(text, 1400), self._secrets)

    def client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google import genai  # imported lazily: SDK is optional at import time
        except ImportError as exc:  # pragma: no cover - dependency issue
            self._genai_import_error = str(exc)
            raise ProviderError(
                self.name,
                "google-genai is not installed. Run: pip install -r requirements.txt",
                kind=OTHER,
            ) from exc
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.api_base:
            from google.genai import types

            kwargs["http_options"] = types.HttpOptions(base_url=self.api_base)
        self._client = genai.Client(**kwargs)
        return self._client

    @staticmethod
    def _classify(exc: Exception) -> tuple[str, int | None, bool]:
        text = f"{type(exc).__name__}: {exc}".lower()
        status: int | None = None
        for code in (400, 401, 403, 404, 429, 500, 502, 503, 504):
            if f" {code}" in text or f"({code}" in text or f"code: {code}" in text:
                status = code
                break
        if "resource_exhausted" in text or "quota" in text or "rate limit" in text:
            return QUOTA, status or 429, True
        if "unauthenticated" in text or "api key not valid" in text or "permission_denied" in text:
            return AUTH, status or 401, False
        if "not_found" in text or "is not found for api version" in text or "not supported" in text:
            return MODEL, status or 404, False
        if "unavailable" in text or "deadline" in text or "timeout" in text or "internal" in text:
            return NETWORK, status or 503, True
        if "invalid_argument" in text or "invalid" in text:
            return OTHER, status or 400, False
        if status and status >= 500:
            return NETWORK, status, True
        if status == 429:
            return QUOTA, status, True
        return OTHER, status, False

    # -- models -------------------------------------------------------------
    def list_models(self, *, refresh: bool = False) -> list[str]:
        if not self.configured():
            return []
        if not refresh and self._models and time.time() - self._models_ts < self.model_cache_ttl:
            return list(self._models)
        try:
            client = self.client()
            found: list[str] = []
            for item in client.models.list():
                name = normalise_model_name(getattr(item, "name", "") or "")
                actions = getattr(item, "supported_actions", None)
                if name and is_text_model(name, actions):
                    found.append(name)
            self._models = sorted(set(found))
            self._models_ts = time.time()
            self._models_error = "" if self._models else "API returned no text models for this key"
        except ProviderError as exc:
            self._models_error = self._clean(exc)
            log.warning("Gemini model listing failed: %s", self._models_error)
        except Exception as exc:  # noqa: BLE001 - SDK raises many types
            self._models_error = self._clean(f"{type(exc).__name__}: {exc}")
            log.warning("Gemini model listing failed: %s", self._models_error)
        return list(self._models)

    def resolve_model(self, *, refresh: bool = False) -> str:
        """Pick the model to use, discovering availability when needed."""
        models = self.list_models(refresh=refresh)
        chosen, reason = select_model(models, self.model)
        self._resolved_reason = reason
        if not chosen:
            raise ProviderError(
                self.name,
                f"no usable text model ({reason}). {self._models_error}".strip(),
                kind=MODEL,
            )
        if chosen != self.model:
            log.info("Gemini model selection: %s", reason)
        self.model = chosen
        return chosen

    def models_error(self) -> str:
        return self._models_error

    def selection_reason(self) -> str:
        return self._resolved_reason

    # -- chat ---------------------------------------------------------------
    def _build_contents(self, turns: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Convert chat turns into Gemini ``contents`` with roles preserved."""
        contents: list[dict[str, Any]] = []
        for turn in turns:
            role = "model" if turn["role"] == "assistant" else "user"
            text = turn.get("content") or ""
            if not text:
                continue
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].append({"text": text})
            else:
                contents.append({"role": role, "parts": [{"text": text}]})
        if not contents:
            contents.append({"role": "user", "parts": [{"text": "Hello"}]})
        if contents[0]["role"] != "user":
            contents.insert(0, {"role": "user", "parts": [{"text": "(conversation continued)"}]})
        return contents

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 3000,
        timeout: int = 120,
    ) -> str:
        if not self.configured():
            raise ProviderError(self.name, "GEMINI_API_KEY is not configured", kind=AUTH)

        from google.genai import types  # local import keeps module import cheap

        system, turns = split_system(messages)
        contents = self._build_contents(turns)
        # Bound every attempt so the retry loop cannot outlive the caller's
        # AI_TIMEOUT (a 503 "high demand" burst otherwise hangs the whole task).
        per_attempt = max(10, min(int(timeout), 50))
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            temperature=temperature,
            max_output_tokens=max_tokens,
            http_options=types.HttpOptions(timeout=per_attempt * 1000),
        )

        last_error: ProviderError | None = None
        model = self.model or self.resolve_model()
        resolved_once = False
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client().models.generate_content(
                    model=model, contents=contents, config=config
                )
            except Exception as exc:  # noqa: BLE001 - SDK exception types vary
                kind, status, retryable = self._classify(exc)
                last_error = ProviderError(
                    self.name,
                    f"{type(exc).__name__}: {self._clean(exc)}",
                    kind=kind,
                    status=status,
                    retryable=retryable,
                    retry_after=parse_retry_after(str(exc)),
                )
                if kind == MODEL and not resolved_once:
                    # The ID may have been retired: re-discover and retry once.
                    resolved_once = True
                    try:
                        model = self.resolve_model(refresh=True)
                    except ProviderError:
                        raise last_error from exc
                    log.warning("Gemini model unavailable; retrying with %s", model)
                    continue
                if not retryable:
                    raise last_error from exc
            else:
                return self._extract_text(response)
            if attempt < self.max_retries:
                delay = backoff_delay(attempt)
                log.warning(
                    "Gemini attempt %s/%s failed (%s); retrying in %ss",
                    attempt,
                    self.max_retries,
                    last_error.detail if last_error else "unknown",
                    delay,
                )
                time.sleep(delay)
        raise last_error or ProviderError(self.name, "unknown failure", kind=OTHER)

    def _extract_text(self, response: Any) -> str:
        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()

        # No text: report *why* instead of a generic "empty response".
        reasons: list[str] = []
        feedback = getattr(response, "prompt_feedback", None)
        block_reason = getattr(feedback, "block_reason", None) if feedback else None
        if block_reason:
            reasons.append(f"prompt blocked: {block_reason}")
        for candidate in getattr(response, "candidates", None) or []:
            finish = getattr(candidate, "finish_reason", None)
            if finish:
                reasons.append(f"finish_reason={finish}")
            safety = getattr(candidate, "safety_ratings", None)
            if safety:
                flagged = [
                    f"{getattr(r, 'category', '?')}={getattr(r, 'probability', '?')}"
                    for r in safety
                    if str(getattr(r, "blocked", False)).lower() == "true"
                ]
                if flagged:
                    reasons.append("safety: " + ", ".join(flagged))
        detail = "; ".join(reasons) or "no candidates returned"
        return_message = getattr(response, "text", None)
        if return_message is not None and not isinstance(return_message, str):
            detail += f" (response.text raised: {type(return_message).__name__})"
        raise ProviderError(self.name, f"no text returned — {detail}", kind=BLOCKED)

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": self.configured(),
            "model": self.model or "(auto)",
            "selection": self._resolved_reason,
            "models_error": self._models_error,
        }
