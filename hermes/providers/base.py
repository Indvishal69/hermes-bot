"""Provider abstraction shared by Groq and Gemini."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

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
