"""AI provider implementations and the fallback router."""

from .base import LLMResult, Provider, ProviderError, split_system
from .gemini import GeminiProvider, is_text_model, select_model
from .groq import GroqProvider, is_chat_model, select_model as select_groq_model
from .router import Router

__all__ = [
    "LLMResult",
    "Provider",
    "ProviderError",
    "split_system",
    "GeminiProvider",
    "GroqProvider",
    "Router",
    "is_text_model",
    "select_model",
    "is_chat_model",
    "select_groq_model",
]


def build_router(settings, *, session=None) -> Router:
    """Construct the router from settings (only configured providers)."""
    providers: dict[str, Provider] = {}
    if settings.groq_api_key:
        providers["groq"] = GroqProvider(
            settings.groq_api_key,
            settings.groq_model,
            session=session,
            model_cache_ttl=settings.model_cache_ttl,
            secrets=settings.secrets,
        )
    if settings.gemini_api_key:
        providers["gemini"] = GeminiProvider(
            settings.gemini_api_key,
            settings.gemini_model,
            api_base=settings.gemini_api_base,
            model_cache_ttl=settings.model_cache_ttl,
            secrets=settings.secrets,
        )
    order = [settings.active_provider] + [name for name in ("groq", "gemini")]
    return Router(
        providers,
        order,
        cooldown=settings.provider_cooldown,
        secrets=settings.secrets,
    )
