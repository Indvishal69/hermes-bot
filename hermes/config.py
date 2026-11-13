"""Environment configuration for Hermes 3.0.

Design rules:
* Everything comes from environment variables. Nothing is hardcoded.
* ``load_settings`` never raises for bad input: it collects every problem so
  the operator sees all of them at once instead of one crash at a time.
* Values that would end up in logs (keys, tokens) are never echoed back.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VALID_PROVIDERS = ("groq", "gemini", "openrouter")


class ConfigError(RuntimeError):
    """Raised when configuration cannot be used at all."""


def _get(env: Mapping[str, str], name: str, default: str = "") -> str:
    value = env.get(name)
    if value is None:
        return default
    return str(value).strip()


def _get_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    problems: list[str],
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = _get(env, name)
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        problems.append(f"{name} must be an integer (got {raw!r}); using {default}.")
        return default
    if value < minimum:
        problems.append(f"{name} must be >= {minimum} (got {value}); using {default}.")
        return default
    if maximum is not None and value > maximum:
        problems.append(f"{name} must be <= {maximum} (got {value}); using {maximum}.")
        return maximum
    return value


def _get_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = _get(env, name).lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on", "y"}


@dataclass(frozen=True)
class Settings:
    """Resolved, immutable runtime settings."""

    # --- identity / access -------------------------------------------------
    discord_token: str = ""
    allowed_user_id: int = 0
    # --- providers ---------------------------------------------------------
    groq_api_key: str = ""
    gemini_api_key: str = ""
    active_provider: str = "groq"
    groq_model: str = "openai/gpt-oss-120b"
    groq_reasoning_effort: str = "low"
    openrouter_api_key: str = ""
    openrouter_model: str = "openrouter/auto"
    openrouter_fallbacks: tuple[str, ...] = ()
    gemini_model: str = ""  # empty => auto-discover a working text model
    gemini_api_base: str = ""
    # --- agent behaviour ---------------------------------------------------
    max_agent_steps: int = 8
    ai_timeout: int = 150
    max_history_messages: int = 10
    temperature: float = 0.2
    max_completion_tokens: int = 1500
    # --- workspace / shell -------------------------------------------------
    data_dir: Path = PROJECT_ROOT / "data"
    workspace_dir: Path = PROJECT_ROOT / "data" / "workspace"
    max_file_bytes: int = 200_000
    max_output_chars: int = 4_000
    command_timeout: int = 45
    allow_git_push: bool = False
    approval_ttl: int = 900
    # --- connectors: YouTube ------------------------------------------------
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_refresh_token: str = ""
    youtube_max_upload_mb: int = 128
    # --- web access --------------------------------------------------------
    web_enabled: bool = True
    web_timeout: int = 20
    web_max_bytes: int = 2_000_000
    web_max_chars: int = 7_000
    web_allowed_domains: tuple[str, ...] = ()
    browser_enabled: bool = False
    browser_timeout: int = 30
    search_provider: str = ""
    search_api_key: str = ""
    searxng_url: str = ""
    # --- service -----------------------------------------------------------
    port: int = 10_000
    log_level: str = "INFO"
    provider_cooldown: int = 60
    quota_cooldown: int = 3_600
    rate_limit_wait_cap: int = 20
    model_cache_ttl: int = 3_600
    task_rate_limit: int = 3
    # --- integrations ------------------------------------------------------
    github_token: str = ""
    render_api_key: str = ""
    vercel_token: str = ""
    cloudflare_api_token: str = ""
    huggingface_token: str = ""
    # --- diagnostics -------------------------------------------------------
    problems: tuple[str, ...] = field(default_factory=tuple)

    # -- derived ------------------------------------------------------------
    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hermes.sqlite3"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def secrets(self) -> tuple[str, ...]:
        """Every secret value that must be redacted from output."""
        candidates = (
            self.discord_token,
            self.groq_api_key,
            self.gemini_api_key,
            self.github_token,
            self.render_api_key,
            self.vercel_token,
            self.cloudflare_api_token,
            self.huggingface_token,
            self.search_api_key,
            self.openrouter_api_key,
            self.youtube_client_secret,
            self.youtube_refresh_token,
        )
        return tuple(value for value in candidates if value and len(value) >= 8)

    @property
    def providers_configured(self) -> dict[str, bool]:
        return {
            "groq": bool(self.groq_api_key),
            "gemini": bool(self.gemini_api_key),
            "openrouter": bool(self.openrouter_api_key),
        }

    def redacted_summary(self) -> dict[str, object]:
        """Safe-to-log snapshot (never includes secret values)."""
        return {
            "active_provider": self.active_provider,
            "groq_model": self.groq_model,
            "gemini_model": self.gemini_model or "(auto)",
            "max_agent_steps": self.max_agent_steps,
            "command_timeout": self.command_timeout,
            "data_dir": str(self.data_dir),
            "workspace_dir": str(self.workspace_dir),
            "providers_configured": self.providers_configured,
            "allow_git_push": self.allow_git_push,
            "web_enabled": self.web_enabled,
            "browser_enabled": self.browser_enabled,
            "search_provider": self.search_provider or "(none)",
            "openrouter_model": self.openrouter_model,
            "groq_reasoning_effort": self.groq_reasoning_effort or "(default)",
            "quota_cooldown": self.quota_cooldown,
        }


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from the environment, collecting problems."""
    env = os.environ if env is None else env
    problems: list[str] = []

    discord_token = _get(env, "DISCORD_TOKEN")
    allowed_raw = _get(env, "ALLOWED_USER_ID")
    allowed_user_id = 0
    if not allowed_raw:
        problems.append("ALLOWED_USER_ID is required (your numeric Discord user ID).")
    elif not allowed_raw.isdigit():
        problems.append("ALLOWED_USER_ID must be numeric (Discord user IDs are digits).")
    else:
        allowed_user_id = int(allowed_raw)

    if not discord_token:
        problems.append("DISCORD_TOKEN is required.")

    groq_key = _get(env, "GROQ_API_KEY")
    gemini_key = _get(env, "GEMINI_API_KEY")
    openrouter_key = _get(env, "OPENROUTER_API_KEY")
    if not groq_key and not gemini_key and not openrouter_key:
        problems.append(
            "Set at least one of GROQ_API_KEY, GEMINI_API_KEY or OPENROUTER_API_KEY."
        )

    active = _get(env, "ACTIVE_PROVIDER", "groq").lower()
    if active not in VALID_PROVIDERS:
        problems.append(
            f"ACTIVE_PROVIDER must be one of {VALID_PROVIDERS} (got {active!r}); using 'groq'."
        )
        active = "groq"
    elif active == "groq" and not groq_key and (gemini_key or openrouter_key):
        fallback = "gemini" if gemini_key else "openrouter"
        problems.append(f"ACTIVE_PROVIDER=groq but GROQ_API_KEY is empty; using '{fallback}'.")
        active = fallback
    elif active == "gemini" and not gemini_key and (groq_key or openrouter_key):
        fallback = "groq" if groq_key else "openrouter"
        problems.append(f"ACTIVE_PROVIDER=gemini but GEMINI_API_KEY is empty; using '{fallback}'.")
        active = fallback
    elif active == "openrouter" and not openrouter_key and (groq_key or gemini_key):
        fallback = "groq" if groq_key else "gemini"
        problems.append(
            f"ACTIVE_PROVIDER=openrouter but OPENROUTER_API_KEY is empty; using '{fallback}'."
        )
        active = fallback

    data_raw = _get(env, "DATA_DIR")
    data_dir = Path(data_raw).expanduser().resolve() if data_raw else PROJECT_ROOT / "data"
    ws_raw = _get(env, "WORKSPACE_DIR")
    workspace_dir = (
        Path(ws_raw).expanduser().resolve() if ws_raw else data_dir / "workspace"
    )

    temperature = 0.2
    temp_raw = _get(env, "TEMPERATURE")
    if temp_raw:
        try:
            temperature = float(temp_raw)
            if not 0.0 <= temperature <= 2.0:
                problems.append("TEMPERATURE must be between 0 and 2; using 0.2.")
                temperature = 0.2
        except ValueError:
            problems.append("TEMPERATURE must be a number; using 0.2.")

    groq_effort = _get(env, "GROQ_REASONING_EFFORT", "low").lower()
    if groq_effort not in ("", "low", "medium", "high"):
        problems.append(
            "GROQ_REASONING_EFFORT must be low, medium or high "
            f"(got {groq_effort!r}); using 'low'."
        )
        groq_effort = "low"

    search_provider = _get(env, "SEARCH_PROVIDER").lower()
    if search_provider not in ("", "tavily", "brave", "searxng"):
        problems.append(
            "SEARCH_PROVIDER must be tavily, brave or searxng "
            f"(got {search_provider!r}); web search stays disabled."
        )
        search_provider = ""
    if search_provider == "searxng" and not _get(env, "SEARXNG_URL"):
        problems.append("SEARCH_PROVIDER=searxng needs SEARXNG_URL.")
        search_provider = ""
    if search_provider in ("tavily", "brave") and not _get(env, "SEARCH_API_KEY"):
        problems.append(f"SEARCH_PROVIDER={search_provider} needs SEARCH_API_KEY.")
        search_provider = ""

    settings = Settings(
        discord_token=discord_token,
        allowed_user_id=allowed_user_id,
        groq_api_key=groq_key,
        gemini_api_key=gemini_key,
        active_provider=active,
        groq_model=_get(env, "GROQ_MODEL", "openai/gpt-oss-120b"),
        groq_reasoning_effort=groq_effort,
        openrouter_api_key=_get(env, "OPENROUTER_API_KEY"),
        openrouter_model=_get(env, "OPENROUTER_MODEL", "openrouter/auto"),
        openrouter_fallbacks=tuple(
            part.strip()
            for part in _get(env, "OPENROUTER_FALLBACK_MODELS").split(",")
            if part.strip()
        ),
        gemini_model=_get(env, "GEMINI_MODEL"),
        gemini_api_base=_get(env, "GEMINI_API_BASE"),
        max_agent_steps=_get_int(env, "MAX_AGENT_STEPS", 8, problems, 1, 25),
        ai_timeout=_get_int(env, "AI_TIMEOUT", 150, problems, 10, 600),
        max_history_messages=_get_int(env, "MAX_HISTORY_MESSAGES", 10, problems, 0, 200),
        temperature=temperature,
        max_completion_tokens=_get_int(env, "MAX_COMPLETION_TOKENS", 1500, problems, 128, 32000),
        data_dir=data_dir,
        workspace_dir=workspace_dir,
        max_file_bytes=_get_int(env, "MAX_FILE_BYTES", 200_000, problems, 1_024, 5_000_000),
        max_output_chars=_get_int(env, "MAX_OUTPUT_CHARS", 4_000, problems, 500, 60_000),
        command_timeout=_get_int(env, "COMMAND_TIMEOUT", 45, problems, 1, 600),
        allow_git_push=_get_bool(env, "ALLOW_GIT_PUSH", False),
        approval_ttl=_get_int(env, "APPROVAL_TTL", 900, problems, 30, 86_400),
        web_enabled=_get_bool(env, "WEB_ENABLED", True),
        web_timeout=_get_int(env, "WEB_TIMEOUT", 20, problems, 5, 120),
        web_max_bytes=_get_int(env, "WEB_MAX_BYTES", 2_000_000, problems, 10_000, 10_000_000),
        web_max_chars=_get_int(env, "WEB_MAX_CHARS", 7_000, problems, 500, 60_000),
        web_allowed_domains=tuple(
            part.strip().lower()
            for part in _get(env, "WEB_ALLOWED_DOMAINS").split(",")
            if part.strip()
        ),
        browser_enabled=_get_bool(env, "BROWSER_ENABLED", False),
        browser_timeout=_get_int(env, "BROWSER_TIMEOUT", 30, problems, 5, 180),
        search_provider=search_provider,
        search_api_key=_get(env, "SEARCH_API_KEY"),
        searxng_url=_get(env, "SEARXNG_URL"),
        port=_get_int(env, "PORT", 10_000, problems, 1, 65_535),
        log_level=_get(env, "LOG_LEVEL", "INFO").upper() or "INFO",
        provider_cooldown=_get_int(env, "PROVIDER_COOLDOWN", 60, problems, 0, 3_600),
        quota_cooldown=_get_int(env, "QUOTA_COOLDOWN", 3_600, problems, 0, 86_400),
        rate_limit_wait_cap=_get_int(env, "RATE_LIMIT_WAIT_CAP", 20, problems, 0, 60),
        model_cache_ttl=_get_int(env, "MODEL_CACHE_TTL", 3_600, problems, 0, 86_400),
        task_rate_limit=_get_int(env, "TASK_RATE_LIMIT", 3, problems, 0, 600),
        github_token=_get(env, "GITHUB_TOKEN"),
        render_api_key=_get(env, "RENDER_API_KEY"),
        vercel_token=_get(env, "VERCEL_TOKEN"),
        cloudflare_api_token=_get(env, "CLOUDFLARE_API_TOKEN"),
        huggingface_token=_get(env, "HF_TOKEN"),
        youtube_client_id=_get(env, "YOUTUBE_CLIENT_ID"),
        youtube_client_secret=_get(env, "YOUTUBE_CLIENT_SECRET"),
        youtube_refresh_token=_get(env, "YOUTUBE_REFRESH_TOKEN"),
        youtube_max_upload_mb=_get_int(env, "YOUTUBE_MAX_UPLOAD_MB", 128, problems, 1, 2_048),
        problems=tuple(problems),
    )
    return settings


def ensure_storage(settings: Settings) -> tuple[Settings, list[str]]:
    """Verify the data directory is usable, falling back to a local one.

    On Render the free plan has no disk, so a blueprint that pointed
    ``DATA_DIR`` at ``/var/data`` makes the process die on startup. Rather than
    crash (and take the bot offline), fall back to ``<project>/data`` and return
    a warning so the operator sees exactly what happened.
    """
    warnings: list[str] = []
    candidates = [settings.data_dir]
    fallback = PROJECT_ROOT / "data"
    if fallback.resolve() != settings.data_dir.resolve():
        candidates.append(fallback)

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".hermes-write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            warnings.append(
                f"DATA_DIR {candidate} is not writable ({type(exc).__name__}: {exc})."
            )
            continue
        if candidate.resolve() != settings.data_dir.resolve():
            warnings.append(
                f"Falling back to DATA_DIR={candidate}. Set DATA_DIR to a writable, "
                "persistent path (attach a Render disk) to keep data across deploys."
            )
            workspace = (
                candidate / "workspace"
                if settings.workspace_dir == settings.data_dir / "workspace"
                else settings.workspace_dir
            )
            return dataclasses.replace(
                settings, data_dir=candidate.resolve(), workspace_dir=workspace
            ), warnings
        return settings, warnings

    warnings.append("No writable data directory found; refusing to start.")
    return settings, warnings
