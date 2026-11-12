"""Shared fixtures. Every test runs against a temporary data directory."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes.config import load_settings  # noqa: E402

BASE_ENV = {
    "DISCORD_TOKEN": "discord-token-value-1234567890",
    "ALLOWED_USER_ID": "123456789012345678",
    "GROQ_API_KEY": "gsk_unit_test_key_1234567890",
    "GEMINI_API_KEY": "AIzaUnitTestKey1234567890abcd",
}


@pytest.fixture()
def settings(tmp_path):
    env = dict(BASE_ENV)
    env["DATA_DIR"] = str(tmp_path / "data")
    return load_settings(env)


@pytest.fixture()
def make_settings(tmp_path):
    def _make(**overrides):
        env = dict(BASE_ENV)
        env["DATA_DIR"] = str(tmp_path / "data")
        env.update(overrides.pop("env", {}))
        return dataclasses.replace(load_settings(env), **overrides)

    return _make


@pytest.fixture()
def workspace(settings):
    from hermes.tools import Workspace

    return Workspace(settings.workspace_dir, max_file_bytes=10_000, max_output_chars=2_000)


@pytest.fixture()
def db(settings):
    from hermes.db import Database

    database = Database(settings.db_path)
    database.init()
    return database


@pytest.fixture()
def public_dns(monkeypatch):
    """Deterministic DNS so URL validation can be tested without the network."""
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return fake_getaddrinfo
