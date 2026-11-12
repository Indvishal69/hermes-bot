"""Provider router: preference, fallback, cooldown and error reporting."""

from __future__ import annotations

import pytest

from hermes.providers.base import Provider, ProviderError
from hermes.providers.router import Router


class FakeProvider(Provider):
    def __init__(self, name: str, *, configured: bool = True, outcomes=None, model="fake-1"):
        self.name = name
        self._configured = configured
        self.model = model
        self.outcomes = list(outcomes or ["ok"])
        self.calls = 0

    def configured(self) -> bool:
        return self._configured

    def complete(self, messages, *, temperature=0.2, max_tokens=3000, timeout=120):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def list_models(self, *, refresh: bool = False):
        return [self.model]

    def current_model(self) -> str:
        return self.model


def test_preferred_provider_is_used_first():
    groq = FakeProvider("groq", outcomes=["from groq"])
    gemini = FakeProvider("gemini", outcomes=["from gemini"])
    router = Router({"groq": groq, "gemini": gemini}, ["groq", "gemini"])
    result = router.complete([{"role": "user", "content": "hi"}])
    assert result.text == "from groq"
    assert result.label == "groq/fake-1"
    assert gemini.calls == 0


def test_fallback_when_preferred_provider_fails():
    groq = FakeProvider(
        "groq", outcomes=[ProviderError("groq", "HTTP 429: rate limited", kind="quota", status=429)]
    )
    gemini = FakeProvider("gemini", outcomes=["from gemini"])
    router = Router({"groq": groq, "gemini": gemini}, ["groq", "gemini"])
    result = router.complete([{"role": "user", "content": "hi"}])
    assert result.text == "from gemini"
    assert result.attempts[0]["provider"] == "groq"
    assert result.attempts[0]["kind"] == "quota"


def test_failed_provider_is_quarantined_then_tried_again_after_cooldown():
    groq = FakeProvider("groq", outcomes=[ProviderError("groq", "boom", kind="network")])
    gemini = FakeProvider("gemini", outcomes=["from gemini"])
    router = Router({"groq": groq, "gemini": gemini}, ["groq", "gemini"], cooldown=60)
    router.complete([{"role": "user", "content": "hi"}])
    status = {row["provider"]: row for row in router.status()}
    assert status["groq"]["cooldown_seconds"] > 0
    # Second call: gemini is tried first because groq is cooling down.
    router.complete([{"role": "user", "content": "hi again"}])
    assert groq.calls == 1
    assert gemini.calls == 2


def test_all_providers_failing_reports_every_error():
    groq = FakeProvider("groq", outcomes=[ProviderError("groq", "HTTP 401 invalid key", kind="auth", status=401)])
    gemini = FakeProvider("gemini", outcomes=[ProviderError("gemini", "HTTP 429 quota", kind="quota", status=429)])
    router = Router({"groq": groq, "gemini": gemini}, ["groq", "gemini"])
    with pytest.raises(ProviderError) as excinfo:
        router.complete([{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "HTTP 401" in message and "HTTP 429" in message


def test_duplicate_names_in_order_are_deduplicated():
    groq = FakeProvider("groq", outcomes=["g"])
    router = Router({"groq": groq}, ["groq", "groq", "groq"])
    assert router.order == ["groq"]
    assert len(router.status()) == 1


def test_unconfigured_provider_is_skipped():
    groq = FakeProvider("groq", configured=False)
    gemini = FakeProvider("gemini", outcomes=["from gemini"])
    router = Router({"groq": groq, "gemini": gemini}, ["groq", "gemini"])
    assert router.available() == ["gemini"]
    assert router.complete([{"role": "user", "content": "hi"}]).provider == "gemini"
    assert groq.calls == 0


def test_no_provider_configured_raises():
    router = Router({"groq": FakeProvider("groq", configured=False)}, ["groq"])
    with pytest.raises(ProviderError):
        router.complete([{"role": "user", "content": "hi"}])


def test_prefer_argument_reorders_within_configured_providers():
    groq = FakeProvider("groq", outcomes=["g"])
    gemini = FakeProvider("gemini", outcomes=["m"])
    router = Router({"groq": groq, "gemini": gemini}, ["groq", "gemini"])
    assert router.complete([{"role": "user", "content": "hi"}], prefer="gemini").provider == "gemini"


def test_success_clears_previous_error():
    groq = FakeProvider(
        "groq",
        outcomes=[ProviderError("groq", "temporary", kind="network"), "recovered"],
    )
    router = Router({"groq": groq}, ["groq"], cooldown=0)
    with pytest.raises(ProviderError):
        router.complete([{"role": "user", "content": "hi"}])
    assert router.status()[0]["last_error"] is not None
    router.complete([{"role": "user", "content": "hi"}])
    assert router.status()[0]["last_error"] is None


def test_unexpected_exception_is_wrapped_not_leaked():
    class Exploding(FakeProvider):
        def complete(self, messages, **kwargs):
            raise RuntimeError("kaboom")

    router = Router({"groq": Exploding("groq")}, ["groq"])
    with pytest.raises(ProviderError) as excinfo:
        router.complete([{"role": "user", "content": "hi"}])
    assert "kaboom" in str(excinfo.value)


def test_secrets_are_redacted_in_recorded_errors():
    secret = "gsk_super_secret_value_123456"
    groq = FakeProvider(
        "groq", outcomes=[ProviderError("groq", f"invalid key {secret}", kind="auth")]
    )
    router = Router({"groq": groq}, ["groq"], secrets=(secret,))
    with pytest.raises(ProviderError):
        router.complete([{"role": "user", "content": "hi"}])
    assert secret not in str(router.status()[0]["last_error"]["message"])


def test_daily_quota_gets_a_long_cooldown():
    quota_error = ProviderError(
        "gemini",
        "ClientError: 429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
        "limit: 20, model: gemini-3.8-flash ... limit exceeded on Free Tier",
        kind="quota",
        status=429,
    )
    groq = FakeProvider("groq", outcomes=["from groq"])
    gemini = FakeProvider("gemini", outcomes=[quota_error, "from gemini"])
    router = Router({"groq": groq, "gemini": gemini}, ["gemini", "groq"],
                    cooldown=60, quota_cooldown=3_600)

    result = router.complete([{"role": "user", "content": "hi"}])
    assert result.provider == "groq"          # fell back immediately

    status = {row["provider"]: row for row in router.status()}
    assert status["gemini"]["cooldown_seconds"] > 3_000
    assert status["gemini"]["last_error"]["daily_quota"] is True


def test_per_minute_rate_limit_keeps_the_short_cooldown():
    burst = ProviderError("gemini", "429 RESOURCE_EXHAUSTED: rate limit per minute",
                          kind="quota", status=429)
    router = Router({"gemini": FakeProvider("gemini", outcomes=[burst, "ok"])},
                    ["gemini"], cooldown=60, quota_cooldown=3_600)
    with pytest.raises(ProviderError):
        router.complete([{"role": "user", "content": "hi"}])
    assert router.status()[0]["cooldown_seconds"] <= 60
    assert router.status()[0]["last_error"]["daily_quota"] is False


def test_short_retry_hint_is_waited_for_instead_of_failing(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr("hermes.providers.router.time.sleep", lambda s: waited.append(s))

    limited = ProviderError(
        "groq",
        "HTTP 429: Rate limit reached ... on tokens per minute (TPM): Limit 8000, "
        "Used 7151, Requested 2068. Please try again in 9.1425s.",
        kind="quota", status=429, retry_after=9.14,
    )
    groq = FakeProvider("groq", outcomes=[limited, "recovered after wait"])
    gemini = FakeProvider("gemini", outcomes=["gemini answer"])
    router = Router({"groq": groq, "gemini": gemini}, ["groq", "gemini"], wait_cap=20)

    result = router.complete([{"role": "user", "content": "hi"}])
    assert result.text == "recovered after wait"
    assert result.provider == "groq"
    assert waited and 9 <= waited[0] <= 10
    assert gemini.calls == 0


def test_long_retry_hint_falls_back_immediately(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr("hermes.providers.router.time.sleep", lambda s: waited.append(s))

    limited = ProviderError("groq", "HTTP 429: rate limit", kind="quota", status=429,
                            retry_after=120.0)
    groq = FakeProvider("groq", outcomes=[limited])
    gemini = FakeProvider("gemini", outcomes=["gemini answer"])
    router = Router({"groq": groq, "gemini": gemini}, ["groq", "gemini"], wait_cap=20)

    assert router.complete([{"role": "user", "content": "hi"}]).provider == "gemini"
    assert waited == []                      # no pointless sleep
    status = {row["provider"]: row for row in router.status()}
    assert status["groq"]["cooldown_seconds"] >= 100


def test_wait_cap_zero_disables_waiting(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr("hermes.providers.router.time.sleep", lambda s: waited.append(s))
    limited = ProviderError("groq", "HTTP 429: slow down", kind="quota", status=429,
                            retry_after=3.0)
    groq = FakeProvider("groq", outcomes=[limited, "never reached"])
    router = Router({"groq": groq}, ["groq"], wait_cap=0)
    with pytest.raises(ProviderError):
        router.complete([{"role": "user", "content": "hi"}])
    assert waited == []


def test_waiting_happens_at_most_twice_per_provider(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr("hermes.providers.router.time.sleep", lambda s: waited.append(s))
    limited = ProviderError("groq", "HTTP 429: TPM limit", kind="quota", status=429,
                            retry_after=2.0)
    groq = FakeProvider("groq", outcomes=[limited, limited, limited, limited])
    router = Router({"groq": groq}, ["groq"], wait_cap=20)
    with pytest.raises(ProviderError):
        router.complete([{"role": "user", "content": "hi"}])
    assert len(waited) == 2
    assert groq.calls == 3
