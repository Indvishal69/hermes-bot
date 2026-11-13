"""Configuration parsing tests."""

from __future__ import annotations

from hermes.config import load_settings


def test_valid_environment_has_no_problems(settings):
    assert settings.ok
    assert settings.allowed_user_id == 123456789012345678
    assert settings.active_provider == "groq"


def test_missing_required_values_are_all_reported():
    settings = load_settings({})
    joined = " ".join(settings.problems)
    assert not settings.ok
    assert "DISCORD_TOKEN" in joined
    assert "ALLOWED_USER_ID" in joined
    assert "GROQ_API_KEY, GEMINI_API_KEY or OPENROUTER_API_KEY" in joined


def test_non_numeric_owner_id_is_rejected():
    settings = load_settings(
        {
            "DISCORD_TOKEN": "t0ken-value-1234567",
            "ALLOWED_USER_ID": "vishal",
            "GROQ_API_KEY": "gsk_key_1234567890",
        }
    )
    assert any("ALLOWED_USER_ID" in problem for problem in settings.problems)


def test_invalid_active_provider_falls_back_to_groq():
    settings = load_settings(
        {
            "DISCORD_TOKEN": "t0ken-value-1234567",
            "ALLOWED_USER_ID": "42",
            "GROQ_API_KEY": "gsk_key_1234567890",
            "ACTIVE_PROVIDER": "openai",
        }
    )
    assert settings.active_provider == "groq"
    assert any("ACTIVE_PROVIDER" in problem for problem in settings.problems)


def test_active_provider_without_key_switches_to_available_provider():
    settings = load_settings(
        {
            "DISCORD_TOKEN": "t0ken-value-1234567",
            "ALLOWED_USER_ID": "42",
            "GEMINI_API_KEY": "AIzaKey1234567890abcd",
            "ACTIVE_PROVIDER": "groq",
        }
    )
    assert settings.active_provider == "gemini"
    assert settings.providers_configured == {"groq": False, "gemini": True, "openrouter": False}


def test_bad_integer_uses_default_and_reports_problem():
    settings = load_settings(
        {
            "DISCORD_TOKEN": "t0ken-value-1234567",
            "ALLOWED_USER_ID": "42",
            "GROQ_API_KEY": "gsk_key_1234567890",
            "MAX_AGENT_STEPS": "many",
            "COMMAND_TIMEOUT": "-5",
        }
    )
    assert settings.max_agent_steps == 8
    assert settings.command_timeout == 45
    assert any("MAX_AGENT_STEPS" in problem for problem in settings.problems)
    assert any("COMMAND_TIMEOUT" in problem for problem in settings.problems)


def test_secrets_excludes_short_and_empty_values(settings):
    assert settings.discord_token in settings.secrets
    assert settings.groq_api_key in settings.secrets
    assert "" not in settings.secrets


def test_redacted_summary_never_leaks_keys(settings):
    summary = str(settings.redacted_summary())
    assert settings.groq_api_key not in summary
    assert settings.discord_token not in summary
    assert settings.gemini_api_key not in summary


def test_data_dir_defaults_inside_project_when_unset():
    settings = load_settings(
        {
            "DISCORD_TOKEN": "t0ken-value-1234567",
            "ALLOWED_USER_ID": "42",
            "GROQ_API_KEY": "gsk_key_1234567890",
        }
    )
    assert settings.data_dir.name == "data"
    assert settings.workspace_dir == settings.data_dir / "workspace"


def test_ensure_storage_falls_back_when_data_dir_is_unwritable(monkeypatch, tmp_path):
    from hermes.config import ensure_storage

    settings = load_settings(
        {
            "DISCORD_TOKEN": "t0ken-value-1234567",
            "ALLOWED_USER_ID": "42",
            "GROQ_API_KEY": "gsk_key_1234567890",
            "DATA_DIR": "/proc/hermes-should-not-exist",
        }
    )
    resolved, warnings = ensure_storage(settings)
    assert resolved.data_dir != settings.data_dir
    assert resolved.workspace_dir == resolved.data_dir / "workspace"
    assert any("Falling back" in warning for warning in warnings)


def test_ensure_storage_keeps_a_writable_data_dir(tmp_path):
    from hermes.config import ensure_storage

    settings = load_settings(
        {
            "DISCORD_TOKEN": "t0ken-value-1234567",
            "ALLOWED_USER_ID": "42",
            "GROQ_API_KEY": "gsk_key_1234567890",
            "DATA_DIR": str(tmp_path / "ok"),
        }
    )
    resolved, warnings = ensure_storage(settings)
    assert resolved.data_dir == settings.data_dir
    assert warnings == []
