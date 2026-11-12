"""Provider router: preference order, automatic fallback, cooldown, reporting."""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

from ..utils import redact
from .base import AUTH, MODEL, Provider, ProviderError, LLMResult

log = logging.getLogger("hermes.providers.router")


class Router:
    """Tries providers in order and remembers why one failed.

    ``cooldown`` seconds of quarantine apply to retryable failures so a dead or
    rate-limited provider is not hammered on every message; auth/model failures
    quarantine for longer because retrying cannot fix them.
    """

    def __init__(
        self,
        providers: dict[str, Provider],
        order: Iterable[str],
        *,
        cooldown: int = 60,
        secrets: tuple[str, ...] = (),
    ):
        self.providers = dict(providers)
        self.order = []
        for name in order:
            if name in self.providers and name not in self.order:
                self.order.append(name)
        for name in self.providers:
            if name not in self.order:
                self.order.append(name)
        self.cooldown = cooldown
        self.secrets = secrets
        self._blocked_until: dict[str, float] = {}
        self._last_error: dict[str, dict[str, Any]] = {}

    # -- state --------------------------------------------------------------
    def available(self) -> list[str]:
        return [name for name in self.order if self.providers[name].configured()]

    def _in_cooldown(self, name: str) -> float:
        until = self._blocked_until.get(name, 0.0)
        remaining = until - time.time()
        return remaining if remaining > 0 else 0.0

    def _record_failure(self, name: str, exc: ProviderError) -> None:
        seconds = self.cooldown if exc.retryable else max(self.cooldown, 300)
        if exc.kind in (AUTH, MODEL):
            seconds = max(seconds, 300)
        if seconds:
            self._blocked_until[name] = time.time() + seconds
        self._last_error[name] = {
            "kind": exc.kind,
            "status": exc.status,
            "message": redact(str(exc), self.secrets),
            "at": time.time(),
        }
        log.warning("Provider %s failed: %s", name, exc.detail)

    def _record_success(self, name: str) -> None:
        self._blocked_until.pop(name, None)
        self._last_error.pop(name, None)

    def reset(self, name: str | None = None) -> None:
        if name:
            self._blocked_until.pop(name, None)
            self._last_error.pop(name, None)
        else:
            self._blocked_until.clear()
            self._last_error.clear()

    # -- chat ---------------------------------------------------------------
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 3000,
        timeout: int = 120,
        prefer: str | None = None,
    ) -> LLMResult:
        candidates = self.available()
        if not candidates:
            raise ProviderError("router", "No AI provider is configured", kind=AUTH)

        order = list(candidates)
        if prefer and prefer in order:
            order.remove(prefer)
            order.insert(0, prefer)
        # Providers in cooldown are tried last, not skipped: a single provider
        # setup must still attempt the call.
        order.sort(key=lambda name: self._in_cooldown(name) > 0)

        attempts: list[dict[str, Any]] = []
        for name in order:
            provider = self.providers[name]
            try:
                text = provider.complete(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
            except ProviderError as exc:
                attempts.append(exc.to_dict())
                self._record_failure(name, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - defensive: never leak a raw crash
                wrapped = ProviderError(name, f"{type(exc).__name__}: {exc}")
                attempts.append(wrapped.to_dict())
                self._record_failure(name, wrapped)
                continue
            self._record_success(name)
            attempts.append({"provider": name, "ok": True})
            return LLMResult(
                text=text,
                provider=name,
                model=provider.current_model(),
                attempts=attempts,
            )

        summary = " | ".join(
            str(item.get("message", "failed")) for item in attempts
        )
        raise ProviderError(
            "router",
            f"All providers failed. {summary}" if summary else "All providers failed.",
            kind="other",
        )

    # -- reporting ----------------------------------------------------------
    def status(self) -> list[dict[str, Any]]:
        rows = []
        for name in self.order:
            provider = self.providers[name]
            info = provider.status()
            info["cooldown_seconds"] = int(self._in_cooldown(name))
            info["last_error"] = self._last_error.get(name)
            info["active"] = self.order[0] == name if self.order else False
            rows.append(info)
        return rows

    def all_models(self, *, refresh: bool = False) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for name in self.order:
            provider = self.providers[name]
            if not provider.configured():
                out[name] = []
                continue
            try:
                out[name] = provider.list_models(refresh=refresh)
            except ProviderError as exc:
                log.warning("Model listing failed for %s: %s", name, exc.detail)
                out[name] = []
        return out
