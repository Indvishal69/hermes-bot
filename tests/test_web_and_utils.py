"""Flask health endpoint and utility helpers."""

from __future__ import annotations

import json

import pytest

from hermes.db import Database
from hermes.tools import Workspace
from hermes.utils import (
    backoff_delay,
    chunk_text,
    extract_json_object,
    human_bytes,
    mask,
    redact,
    truncate,
)
from hermes.web import create_app


class FakeRouter:
    def status(self):
        return [
            {"provider": "groq", "configured": True, "model": "llama-3.3-70b-versatile"},
            {"provider": "gemini", "configured": False, "model": "(auto)"},
        ]


def test_health_endpoint_reports_ok(settings):
    db = Database(settings.db_path)
    db.init()
    workspace = Workspace(settings.workspace_dir)
    app = create_app(settings, router=FakeRouter(), db=db, workspace=workspace)
    client = app.test_client()
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["version"].startswith("3.")
    assert payload["connectors_active"] == 0
    assert payload["providers_configured"] == ["groq"]


def test_health_response_leaks_no_paths_or_secrets(settings):
    db = Database(settings.db_path)
    db.init()
    workspace = Workspace(settings.workspace_dir)
    app = create_app(settings, router=FakeRouter(), db=db, workspace=workspace)
    body = json.dumps(app.test_client().get("/").get_json())
    assert str(settings.workspace_dir) not in body
    assert str(settings.db_path) not in body
    assert settings.groq_api_key not in body
    assert settings.discord_token not in body


def test_health_endpoint_reports_unhealthy_database(settings, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    db = Database(blocker / "db.sqlite3")  # cannot create a directory under a file
    app = create_app(settings, router=FakeRouter(), db=db, workspace=None)
    response = app.test_client().get("/health")
    assert response.status_code == 503
    assert response.get_json()["database"] is False


def test_index_and_ready_routes(settings):
    app = create_app(settings, router=FakeRouter())
    client = app.test_client()
    assert client.get("/ready").get_json()["ready"] is True
    assert client.get("/").get_json()["service"] == "Hermes 3.0"


# -- utils ------------------------------------------------------------------
def test_redact_removes_known_and_shaped_secrets():
    text = "key=AIzaSyD-veryLongFakeKeyValue1234567890 and gsk_abcdefghijklmnopqrstuvwx"
    cleaned = redact(text)
    assert "AIzaSyD" not in cleaned
    assert "gsk_" not in cleaned
    assert "[REDACTED]" in cleaned


def test_redact_removes_explicit_secret():
    assert redact("token=abc123secretvalue", ["abc123secretvalue"]) == "token=[REDACTED]"


def test_extract_json_object_variants():
    assert extract_json_object('{"action":"final","message":"ok"}')["action"] == "final"
    assert extract_json_object('```json\n{"a":1}\n```')["a"] == 1
    assert extract_json_object('Sure! {"a": 2} hope that helps')["a"] == 2
    with pytest.raises(ValueError):
        extract_json_object("no json here")
    with pytest.raises(ValueError):
        extract_json_object('{"broken": ')


def test_backoff_grows_and_caps():
    assert backoff_delay(1, jitter=0) == 1.5
    assert backoff_delay(2, jitter=0) == 2.25
    assert backoff_delay(20, jitter=0) == 20.0


def test_truncate_and_chunk():
    assert truncate("abcdefghij", 5) == "abcde"          # too small for a marker
    assert "truncated" in truncate("x" * 200, 30)
    assert chunk_text("abcdef", 2) == ["ab", "cd", "ef"]


def test_human_helpers():
    assert human_bytes(999) == "999 B"
    assert human_bytes(2048) == "2.0 KB"
    assert mask("") == "missing"
    assert mask("short") == "configured"
    assert mask("a" * 12).endswith("aaaa)")
