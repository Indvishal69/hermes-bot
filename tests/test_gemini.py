"""Gemini: model discovery/selection, content structure, error handling."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes.providers.base import ProviderError
from hermes.providers.gemini import (
    GeminiProvider,
    is_text_model,
    normalise_model_name,
    select_model,
)


# -- pure selection logic ---------------------------------------------------
def test_normalise_model_name_strips_prefix():
    assert normalise_model_name("models/gemini-3.8-flash") == "gemini-3.8-flash"
    assert normalise_model_name("gemini-3.8-flash") == "gemini-3.8-flash"
    assert normalise_model_name("") == ""


@pytest.mark.parametrize(
    "name,expected",
    [
        ("gemini-3.8-flash", True),
        ("models/gemini-3.6-flash", True),
        ("gemini-3.1-pro-preview", True),
        ("text-embedding-004", False),
        ("gemini-3.8-flash-tts", False),
        ("gemini-3.8-live", False),
        ("gemini-3.1-flash-image", False),
        ("gemini-3.5-transcribe", False),
        ("gemini-2.5-computer-use-preview-10-2025", False),
        ("", False),
    ],
)
def test_is_text_model(name, expected):
    assert is_text_model(name) is expected


def test_is_text_model_checks_supported_actions():
    assert is_text_model("gemini-custom", ["generateContent"]) is True
    assert is_text_model("gemini-custom", ["embedContent"]) is False


def test_configured_model_used_when_available():
    chosen, reason = select_model(["gemini-3.6-flash", "gemini-3.8-flash"], "gemini-3.6-flash")
    assert chosen == "gemini-3.6-flash"
    assert "configured model is available" in reason


def test_missing_configured_model_falls_back_to_preferred():
    chosen, reason = select_model(["gemini-3.8-flash", "gemini-3.5-flash-lite"], "gemini-9.9-ultra")
    assert chosen == "gemini-3.8-flash"
    assert "not available" in reason


def test_auto_selection_without_configured_model():
    chosen, reason = select_model(["gemini-3.5-flash-lite", "gemini-3.6-flash"], "")
    assert chosen == "gemini-3.6-flash"
    assert "auto-selected" in reason


def test_unknown_family_falls_back_to_any_flash_model():
    chosen, _ = select_model(["gemini-experimental-flash", "gemini-experimental-pro"], "")
    assert chosen == "gemini-experimental-flash"


def test_empty_list_with_configured_model_still_tries_it():
    chosen, reason = select_model([], "gemini-3.8-flash")
    assert chosen == "gemini-3.8-flash"
    assert "list unavailable" in reason


def test_empty_list_without_configured_model_returns_none():
    chosen, _ = select_model([], "")
    assert chosen is None


# -- SDK interaction --------------------------------------------------------
class FakeModelInfo:
    def __init__(self, name, actions=("generateContent",)):
        self.name = name
        self.supported_actions = list(actions)


class FakeModels:
    def __init__(self, infos, responses):
        self.infos = infos
        self.responses = list(responses)
        self.calls: list[dict] = []

    def list(self):
        return list(self.infos)

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, infos, responses):
        self.models = FakeModels(infos, responses)


def make_provider(infos, responses, model=""):
    client = FakeClient(infos, responses)
    provider = GeminiProvider("AIzaTestKey1234567890", model, client=client)
    return provider, client


def test_complete_preserves_roles_and_system_instruction():
    provider, client = make_provider(
        [FakeModelInfo("models/gemini-3.8-flash")],
        [SimpleNamespace(text="hello there", candidates=[], prompt_feedback=None)],
    )
    messages = [
        {"role": "system", "content": "You are Hermes."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert provider.complete(messages) == "hello there"
    call = client.models.calls[0]
    assert call["model"] == "gemini-3.8-flash"
    assert [item["role"] for item in call["contents"]] == ["user", "model", "user"]
    assert call["contents"][0]["parts"][0]["text"] == "first"
    assert call["config"].system_instruction == "You are Hermes."


def test_consecutive_same_role_messages_are_merged():
    provider, client = make_provider(
        [FakeModelInfo("gemini-3.8-flash")],
        [SimpleNamespace(text="ok", candidates=[], prompt_feedback=None)],
    )
    provider.complete(
        [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
        ]
    )
    contents = client.models.calls[0]["contents"]
    assert len(contents) == 1
    assert [part["text"] for part in contents[0]["parts"]] == ["a", "b"]


def test_retired_model_triggers_rediscovery_and_retry():
    provider, client = make_provider(
        [FakeModelInfo("models/gemini-3.8-flash")],
        [
            Exception("404 NOT_FOUND: model gemini-old is not found for API version v1beta"),
            SimpleNamespace(text="recovered", candidates=[], prompt_feedback=None),
        ],
        model="gemini-old",
    )
    assert provider.complete([{"role": "user", "content": "hi"}]) == "recovered"
    assert len(client.models.calls) == 2
    assert client.models.calls[1]["model"] == "gemini-3.8-flash"


def test_auth_error_is_not_retried():
    provider, client = make_provider(
        [FakeModelInfo("gemini-3.8-flash")],
        [Exception("401 UNAUTHENTICATED: API key not valid. Please pass a valid API key.")],
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.kind == "auth"
    assert len(client.models.calls) == 1


def test_quota_error_is_classified_as_retryable():
    provider = GeminiProvider("AIzaKey1234567890")
    kind, status, retryable = provider._classify(
        Exception("429 RESOURCE_EXHAUSTED: quota exceeded for model")
    )
    assert kind == "quota"
    assert retryable is True


def test_model_error_classification():
    provider = GeminiProvider("AIzaKey1234567890")
    kind, _, retryable = provider._classify(Exception("404 NOT_FOUND: model not supported"))
    assert kind == "model"
    assert retryable is False


def test_blocked_response_reports_finish_reason():
    provider, _ = make_provider(
        [FakeModelInfo("gemini-3.8-flash")],
        [
            SimpleNamespace(
                text=None,
                candidates=[SimpleNamespace(finish_reason="SAFETY", safety_ratings=None)],
                prompt_feedback=None,
            )
        ],
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.kind == "blocked"
    assert "SAFETY" in str(excinfo.value)


def test_model_listing_error_is_surfaced():
    class BrokenModels:
        def list(self):
            raise RuntimeError("403 PERMISSION_DENIED: Generative Language API not enabled")

    client = SimpleNamespace(models=BrokenModels())
    provider = GeminiProvider("AIzaKey1234567890", client=client)
    assert provider.list_models() == []
    assert "PERMISSION_DENIED" in provider.models_error()
