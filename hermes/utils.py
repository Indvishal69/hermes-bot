"""Small, dependency-free helpers shared across Hermes."""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Iterable, Mapping

# Patterns for credentials that must never reach logs or Discord, even when the
# exact secret value is not known to the redactor (e.g. a pasted key).
_GENERIC_SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),          # Google API keys
    re.compile(r"gsk_[0-9A-Za-z]{20,}"),             # Groq keys
    re.compile(r"sk-[0-9A-Za-z_\-]{16,}"),           # generic bearer keys
    re.compile(r"gh[pousr]_[0-9A-Za-z]{20,}"),       # GitHub tokens
    re.compile(r"hf_[0-9A-Za-z]{20,}"),              # Hugging Face tokens
    re.compile(r"Bearer\s+[0-9A-Za-z._\-]{16,}", re.I),
)
REDACTED = "[REDACTED]"


def redact(text: Any, secrets: Iterable[str] = ()) -> str:
    """Remove known secret values and obvious credential shapes from *text*."""
    out = "" if text is None else str(text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, REDACTED)
    for pattern in _GENERIC_SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def truncate(text: Any, limit: int, *, marker: str = "\n…[truncated]") -> str:
    out = "" if text is None else str(text)
    if limit <= 0 or len(out) <= limit:
        return out
    if limit <= len(marker):
        return out[:limit]
    keep = max(0, limit - len(marker))
    return out[:keep] + marker


def backoff_delay(attempt: int, base: float = 1.5, cap: float = 20.0, jitter: float = 0.25) -> float:
    """Exponential backoff with jitter for retryable API failures."""
    attempt = max(1, int(attempt))
    delay = min(cap, base ** attempt)
    if jitter:
        delay += random.uniform(0, delay * jitter)
    return round(min(cap, delay), 3)


_RETRY_PATTERNS = (
    re.compile(r"try again in (\d+(?:\.\d+)?)\s*s", re.I),
    re.compile(r"retry (?:again )?in (\d+(?:\.\d+)?)\s*s", re.I),
    re.compile(r"retryDelay\"?\s*[:=]\s*\"?(\d+(?:\.\d+)?)s", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*seconds?\b", re.I),
)


def parse_retry_after(text: Any = None, headers: Mapping[str, Any] | None = None) -> float | None:
    """Extract a server's "wait N seconds" hint.

    Providers say this in different places — a ``Retry-After`` header, a
    ``retryDelay`` field, or prose like "Please try again in 9.1425s". Honouring
    it is the difference between a task that recovers and one that fails.
    """
    if headers:
        for key in ("Retry-After", "retry-after", "X-RateLimit-Reset-Requests"):
            raw = headers.get(key)
            if raw is None:
                continue
            try:
                value = float(str(raw).strip().rstrip("s"))
            except (TypeError, ValueError):
                continue
            if 0 <= value <= 86_400:
                return value
    if not text:
        return None
    haystack = str(text)[:4_000]
    for pattern in _RETRY_PATTERNS:
        match = pattern.search(haystack)
        if match:
            try:
                value = float(match.group(1))
            except (TypeError, ValueError):
                continue
            if 0 <= value <= 86_400:
                return round(value, 2)
    return None


def extract_json_object(raw: str) -> dict[str, Any]:
    """Parse the single JSON object a model was asked to return.

    Tolerates ```json fences and leading/trailing prose, but never invents
    content: if no JSON object can be parsed, ValueError is raised.
    """
    if raw is None:
        raise ValueError("Model returned an empty response.")
    text = str(raw).strip()
    if not text:
        raise ValueError("Model returned an empty response.")
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("Model did not return a JSON object.") from None
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Model returned malformed JSON: {exc.msg}") from None
    if not isinstance(obj, dict):
        raise ValueError("Model returned JSON that is not an object.")
    return obj


def chunk_text(text: str, size: int = 1850) -> list[str]:
    """Split text into Discord-safe chunks without cutting mid-code-block."""
    text = "" if text is None else str(text)
    if size <= 0:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def human_bytes(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def human_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def utc_now() -> float:
    return time.time()


def mask(value: str) -> str:
    """Show that a credential exists without revealing it."""
    if not value:
        return "missing"
    if len(value) <= 8:
        return "configured"
    return f"configured (…{value[-4:]})"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def flatten_mapping(data: Mapping[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten nested dicts for compact display."""
    out: dict[str, str] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            out.update(flatten_mapping(value, prefix=f"{name}."))
        else:
            out[name] = str(value)
    return out
