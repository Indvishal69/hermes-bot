"""OpenRouter provider: model chain, rotation on model-level failures."""

from __future__ import annotations

import json

import pytest

from hermes.config import load_settings
from hermes.providers.base import ProviderError
from hermes.providers.openrouter import AUTO_MODEL, OpenRouterProvider, build_model_chain


class FakeResponse:
    def __init__(self, status_code: int, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, *, models=None, posts=None):
        self.models = models or {"data": [{"id": "openrouter/auto"}, {"id": "acme/alpha"},
                                          {"id": "acme/beta"}]}
        self.posts = list(posts or [])
        self.get_calls = 0
        self.post_calls: list[dict] = []

    def get(self, url, **kwargs):
        self.get_calls += 1
        return FakeResponse(200, self.models)

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls.append(json or {})
        outcome = self.posts.pop(0) if self.posts else (
            200, {"choices": [{"message": {"content": "ok"}}]}
        )
        return FakeResponse(*outcome)


def make_provider(session, model=AUTO_MODEL, fallbacks=()):
    return OpenRouterProvider("sk-or-unit-test", model, fallbacks=fallbacks, session=session)


def test_model_chain_is_deduplicated_and_keeps_primary_first():
    assert build_model_chain("acme/a", ["acme/b", "acme/a", "", "  "]) == ["acme/a", "acme/b"]
    assert build_model_chain("", []) == [AUTO_MODEL]


def test_default_model_is_openrouter_auto():
    settings = load_settings(
        {
            "DISCORD_TOKEN": "t0ken-value-1234567",
            "ALLOWED_USER_ID": "42",
            "OPENROUTER_API_KEY": "sk-or-x",
        }
    )
    assert settings.openrouter_model == AUTO_MODEL
    assert settings.providers_configured["openrouter"] is True


def test_fallbacks_come_from_a_comma_separated_env_var():
    settings = load_settings(
        {
            "DISCORD_TOKEN": "t0ken-value-1234567",
            "ALLOWED_USER_ID": "42",
            "OPENROUTER_API_KEY": "sk-or-x",
            "OPENROUTER_FALLBACK_MODELS": "acme/a, acme/b",
        }
    )
    assert settings.openrouter_fallbacks == ("acme/a", "acme/b")


def test_completion_uses_the_primary_model():
    session = FakeSession(posts=[(200, {"choices": [{"message": {"content": "hello"}}]})])
    provider = make_provider(session, model="acme/alpha")
    assert provider.complete([{"role": "user", "content": "hi"}]) == "hello"
    assert session.post_calls[0]["model"] == "acme/alpha"
    assert provider.current_model() == "acme/alpha"


def test_model_level_failure_rotates_to_the_next_model():
    session = FakeSession(posts=[
        (404, {"error": {"message": "No endpoints found for acme/alpha"}}),
        (200, {"choices": [{"message": {"content": "from beta"}}]}),
    ])
    provider = make_provider(session, model="acme/alpha", fallbacks=["acme/beta"])
    assert provider.complete([{"role": "user", "content": "hi"}]) == "from beta"
    assert [call["model"] for call in session.post_calls] == ["acme/alpha", "acme/beta"]
    assert "rotated to acme/beta" in provider.selection_reason()


def test_gated_model_403_also_rotates():
    session = FakeSession(posts=[
        (403, {"error": {"message": "model is not available in your region"}}),
        (200, {"choices": [{"message": {"content": "from beta"}}]}),
    ])
    provider = make_provider(session, model="acme/alpha", fallbacks=["acme/beta"])
    assert provider.complete([{"role": "user", "content": "hi"}]) == "from beta"


def test_auth_error_stops_immediately():
    session = FakeSession(posts=[(401, {"error": {"message": "No auth credentials found"}})])
    provider = make_provider(session, model="acme/alpha", fallbacks=["acme/beta"])
    with pytest.raises(ProviderError) as excinfo:
        provider.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.kind == "auth"
    assert len(session.post_calls) == 1


def test_rate_limit_carries_the_retry_hint():
    session = FakeSession(posts=[
        (429, {"error": {"message": "rate limited, try again in 7s"}}),
        (429, {"error": {"message": "rate limited, try again in 7s"}}),
    ])
    provider = make_provider(session, model="acme/alpha")
    with pytest.raises(ProviderError) as excinfo:
        provider.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.retry_after == 7.0


def test_empty_content_is_reported():
    session = FakeSession(posts=[
        (200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}),
        (200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}),
    ])
    provider = make_provider(session, model="acme/alpha")
    with pytest.raises(ProviderError) as excinfo:
        provider.complete([{"role": "user", "content": "hi"}])
    assert "length" in str(excinfo.value)


def test_content_parts_are_joined():
    session = FakeSession(posts=[
        (200, {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}),
    ])
    provider = make_provider(session)
    assert provider.complete([{"role": "user", "content": "hi"}]) == "ab"


def test_model_listing_and_cache():
    session = FakeSession()
    provider = make_provider(session)
    assert "acme/alpha" in provider.list_models()
    provider.list_models()
    assert session.get_calls == 1


def test_missing_model_falls_back_to_a_listed_chain_member():
    session = FakeSession(models={"data": [{"id": "acme/beta"}]})
    provider = make_provider(session, model="acme/retired", fallbacks=["acme/beta"])
    assert provider.resolve_model() == "acme/beta"


def test_insufficient_credits_rotates_and_explains():
    session = FakeSession(posts=[
        (402, {"error": {"message": "Insufficient credits"}}),
        (200, {"choices": [{"message": {"content": "from the free model"}}]}),
    ])
    provider = make_provider(session, model="anthropic/claude-expensive",
                             fallbacks=["acme/free-model:free"])
    assert provider.complete([{"role": "user", "content": "hi"}]) == "from the free model"
    assert session.post_calls[1]["model"] == "acme/free-model:free"


def test_credits_error_without_a_fallback_says_what_to_do():
    session = FakeSession(posts=[(402, {"error": {"message": "Insufficient credits"}})])
    provider = make_provider(session, model="anthropic/claude-expensive")
    with pytest.raises(ProviderError) as excinfo:
        provider.complete([{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "no credits" in message and ":free" in message
