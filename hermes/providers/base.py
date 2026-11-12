"""Provider abstraction shared by Groq and Gemini."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# Error kinds, used to decide whether a fallback/retry makes sense.
AUTH = "auth"
QUOTA = "quota"
MODEL = "model"
NETWORK = "network"
BLOCKED = "blocked"
CONFIG = "config"
OTHER = "other"

RETRYABLE_KINDS = {QUOTA, NETWORK}


class ProviderError(RuntimeError):
    """A provider failure with enough structure to report it accurately."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        kind: str = OTHER,
        status: int | None = None,
        retryable: bool | None = None,
    ):
        self.provider = provider
        self.kind = kind
        self.status = status
        self.retryable = kind in RETRYABLE_KINDS if retryable is None else retryable
        super().__init__(f"{provider}: {message}")

    @property
    def detail(self) -> str:
        label = f"[{self.kind}"
        if self.status:
            label += f" HTTP {self.status}"
        label += "]"
        return f"{label} {super().__str__()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "kind": self.kind,
            "status": self.status,
            "message": str(self),
        }


@dataclass
class LLMResult:
    """A completed chat call."""

    text: str
    provider: str
    model: str
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


class Provider(ABC):
    """Minimal chat interface every provider must implement."""

    name: str = "provider"

    @abstractmethod
    def configured(self) -> bool:
        """True when credentials for this provider are present."""

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 3000,
        timeout: int = 120,
    ) -> str:
        """Return the assistant text for *messages*."""

    @abstractmethod
    def list_models(self, *, refresh: bool = False) -> list[str]:
        """Return text-capable model IDs available to this API key."""

    def current_model(self) -> str:
        return getattr(self, "model", "") or ""

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": self.configured(),
            "model": self.current_model(),
        }


def choose_model(
    available: Iterable[str],
    configured: str,
    *,
    preferred: Iterable[str],
    usable: Callable[[str], bool],
    label: str = "model",
    fallback_key: Callable[[str], Any] | None = None,
) -> tuple[str | None, str]:
    """Pick a model ID the API key can actually use.

    Order: the configured model if the API lists it, then the preferred list,
    then any usable model (optionally ranked by ``fallback_key``). Returns
    ``(model, reason)``; ``model`` is ``None`` only when nothing can be used.
    """
    configured = (configured or "").strip()
    names = [str(name).strip() for name in available if str(name).strip()]

    if not names:
        if configured:
            return configured, "model list unavailable; trying the configured model"
        return None, "no models reported by the API"

    if configured and configured in names:
        return configured, "configured model is available"

    preferred_name = next((name for name in preferred if name in names), None)
    if preferred_name:
        reason = (
            f"configured model '{configured}' is not available; using {preferred_name}"
            if configured
            else f"auto-selected {preferred_name}"
        )
        return preferred_name, reason

    remaining = [name for name in names if usable(name)]
    if remaining:
        chosen = min(remaining, key=fallback_key) if fallback_key else remaining[0]
        return chosen, f"auto-selected {chosen} (no preferred model listed)"

    return None, f"the API key exposes no usable {label}"


def split_system(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    """Separate system instructions from the conversation turns."""
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []
    for message in messages:
        role = (message.get("role") or "user").lower()
        content = message.get("content") or ""
        if role == "system":
            if content.strip():
                system_parts.append(content.strip())
        else:
            turns.append({"role": role, "content": content})
    return "\n\n".join(system_parts), turns
