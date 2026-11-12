"""Groq provider: model discovery, retirement handling, retries, payload shape.

Motivated by a live failure: ``llama-3.3-70b-versatile`` was shut down for free
and developer tiers on 16 August 2026, so every task failed with
``HTTP 404 model_not_found``. The provider now checks the account's model list
and switches to a working chat model instead of trusting a hardcoded ID.
"""

from __future__ import annotations

import json

import pytest

import hermes.providers.groq as groq_module
from hermes.config import load_settings
from hermes.providers.base import ProviderError
from hermes.providers.groq import GroqProvider, is_chat_model, select_model


# -- pure selection logic ---------------------------------------------------
@pytest.mark.parametrize(
    "name,expected",
    [
        ("openai/gpt-oss-120b", True),
        ("openai/gpt-oss-20b", True),
        ("qwen/qwen3.8-27b", True),
        ("llama-3.3-70b-versatile", True),
        ("whisper-large-v3", False),
        ("whisper-large-v3-turbo", False),
        ("canopylabs/orpheus-v1-english", False),
        ("meta-llama/llama-prompt-guard-2-22m", False),
        ("openai/gpt-oss-safeguard-20b", False),
        ("", False),
    ],
)
def test_is_chat_model(name, expected):
    assert is_chat_model(name) is expected


def test_configured_model_used_when_available():
    chosen, reason = select_model(["openai/gpt-oss-120b", "openai/gpt-oss-20b"], "openai/gpt-oss-20b")
    assert chosen == "openai/gpt-oss-20b"
    assert "configured model is available" in reason


def test_retired_model_falls_back_to_preferred_replacement():
    chosen, reason = select_model(
        ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "whisper-large-v3"],
        "llama-3.3-70b-versatile",
    )
    assert chosen == "openai/gpt-oss-120b"
    assert "not available" in reason


def test_auto_selection_without_configured_model():
    chosen, reason = select_model(["openai/gpt-oss-20b", "openai/gpt-oss-120b"], "")
    assert chosen == "openai/gpt-oss-120b"
    assert "auto-selected" in reason


def test_unlisted_family_falls_back_to_any_chat_model():
    chosen, _ = select_model(["acme/experimental-chat"], "")
    assert chosen == "acme/experimental-chat"


def test_only_non_chat_models_means_no_choice():
    chosen, reason = select_model(["whisper-large-v3"], "")
    assert chosen is None
    assert "no usable chat model" in reason


def test_empty_list_keeps_configured_model_as_last_resort():
    chosen, reason = select_model([], "openai/gpt-oss-120b")
    assert chosen == "openai/gpt-oss-120b"
    assert "list unavailable" in reason


def test_default_groq_model_is_not_a_retired_id():
    settings = load_settings(
        {
            "DISCORD_TOKEN": "t0ken-value-1234567",
            "ALLOWED_USER_ID": "42",
            "GROQ_API_KEY": "gsk_key_1234567890",
        }
    )
    assert settings.groq_model == "openai/gpt-oss-120b"


# -- HTTP behaviour ---------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, *, models=None, posts=None):
        self.models = models if models is not None else {
            "data": [{"id": "openai/gpt-oss-120b"}, {"id": "openai/gpt-oss-20b"},
                     {"id": "whisper-large-v3"}]
        }
        self.posts = list(posts or [])
        self.post_calls: list[dict] = []
        self.get_calls = 0

    def get(self, url, headers=None, timeout=None):
        self.get_calls += 1
        return FakeResponse(200, self.models)

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls.append(json)
        outcome = self.posts.pop(0) if self.posts else (200, {"choices": [{"message": {"content": "ok"}}]})
        return FakeResponse(*outcome)


def make_provider(session, model="openai/gpt-oss-120b"):
    return GroqProvider("gsk_unit_test_key_123456", model, session=session)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(groq_module.time, "sleep", lambda _seconds: None)


def test_list_models_returns_all_ids_including_audio():
    session = FakeSession()
    provider = make_provider(session)
    assert provider.list_models() == ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "whisper-large-v3"]


def test_model_listing_is_cached():
    session = FakeSession()
    provider = make_provider(session)
    provider.list_models()
    provider.list_models()
    assert session.get_calls == 1


def test_retired_model_is_replaced_and_the_call_retried():
    session = FakeSession(
        posts=[
            (404, {"error": {"message": "The model `llama-3.3-70b-versatile` does not exist "
                                       "or you do not have access to it.",
                             "code": "model_not_found"}}),
            (200, {"choices": [{"message": {"content": "recovered"}}]}),
        ]
    )
    provider = make_provider(session, model="llama-3.3-70b-versatile")
    assert provider.complete([{"role": "user", "content": "hi"}]) == "recovered"
    assert provider.model == "openai/gpt-oss-120b"
    assert session.post_calls[0]["model"] == "llama-3.3-70b-versatile"
    assert session.post_calls[1]["model"] == "openai/gpt-oss-120b"


def test_rate_limit_is_retried_then_succeeds():
    session = FakeSession(
        posts=[
            (429, {"error": {"message": "rate limit"}}),
            (200, {"choices": [{"message": {"content": "after retry"}}]}),
        ]
    )
    provider = make_provider(session)
    assert provider.complete([{"role": "user", "content": "hi"}]) == "after retry"
    assert len(session.post_calls) == 2


def test_auth_error_is_not_retried():
    session = FakeSession(posts=[(401, {"error": {"message": "Invalid API Key"}})])
    provider = make_provider(session)
    with pytest.raises(ProviderError) as excinfo:
        provider.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.kind == "auth"
    assert len(session.post_calls) == 1


def test_payload_separates_system_and_uses_completion_tokens():
    session = FakeSession()
    provider = make_provider(session)
    provider.complete(
        [
            {"role": "system", "content": "You are Hermes."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "again"},
        ],
        temperature=0.1,
        max_tokens=123,
    )
    payload = session.post_calls[0]
    assert payload["messages"][0] == {"role": "system", "content": "You are Hermes."}
    assert [m["role"] for m in payload["messages"]] == ["system", "user", "assistant", "user"]
    assert payload["max_completion_tokens"] == 123
    assert payload["temperature"] == 0.1
    assert "max_tokens" not in payload


def test_empty_message_is_reported_with_finish_reason():
    session = FakeSession(
        posts=[(200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})]
    )
    provider = make_provider(session)
    with pytest.raises(ProviderError) as excinfo:
        provider.complete([{"role": "user", "content": "hi"}])
    assert "length" in str(excinfo.value)


def test_model_listing_failure_is_surfaced_and_configured_model_is_used():
    class BrokenSession(FakeSession):
        def get(self, url, headers=None, timeout=None):
            self.get_calls += 1
            return FakeResponse(403, {"error": {"message": "PERMISSION_DENIED"}})

    session = BrokenSession(posts=[(200, {"choices": [{"message": {"content": "still works"}}]})])
    provider = make_provider(session, model="openai/gpt-oss-120b")
    # A failed listing must not block the call: the configured model is tried.
    assert provider.list_models() == []
    assert "403" in provider.models_error()
    assert provider.complete([{"role": "user", "content": "hi"}]) == "still works"


def test_unconfigured_provider_refuses_to_call():
    provider = GroqProvider("", "openai/gpt-oss-120b", session=FakeSession())
    assert provider.configured() is False
    assert provider.list_models() == []
    with pytest.raises(ProviderError):
        provider.complete([{"role": "user", "content": "hi"}])
