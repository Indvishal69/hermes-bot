"""Database: migrations, memory, tasks, audit, backup/restore."""

from __future__ import annotations

from hermes.db import SCHEMA_VERSION, Database


def test_init_is_idempotent_and_reaches_latest_schema(db, settings):
    assert db.schema_version() == SCHEMA_VERSION
    assert db.init() == SCHEMA_VERSION
    assert db.healthy()


def test_migrating_an_existing_v1_database_upgrades_it(tmp_path):
    path = tmp_path / "old.sqlite3"
    database = Database(path)
    database.init()
    # Simulate a database created before migration 2 existed.
    with database.connect() as conn:
        conn.execute("DROP TABLE IF EXISTS project_memory")
        conn.execute("DELETE FROM schema_migrations WHERE version = 2")
    assert database.schema_version() == 1
    upgraded = Database(path)
    assert upgraded.init() == SCHEMA_VERSION
    upgraded.add_note("demo", "note survives upgrade")
    assert upgraded.notes("demo")[0]["note"] == "note survives upgrade"


def test_chat_memory_roundtrip_and_trimming(settings):
    database = Database(settings.db_path, max_messages_per_user=4)
    database.init()
    for index in range(6):
        database.add_message(7, "user", f"message {index}")
    history = database.history(7, limit=10)
    assert [item["content"] for item in history] == ["message 2", "message 3", "message 4", "message 5"]


def test_history_limit_and_clear(db):
    db.add_message(1, "user", "hello")
    db.add_message(1, "assistant", "hi")
    assert len(db.history(1, limit=1)) == 1
    assert db.clear_history(1) == 2
    assert db.history(1) == []


def test_project_memory_keeps_newest_notes(db):
    for index in range(3):
        db.add_note("proj", f"note {index}")
    notes = db.notes("proj")
    assert notes[0]["note"] == "note 2"
    assert db.clear_notes("proj") == 3


def test_task_lifecycle(db):
    task_id = db.start_task(5, "build a landing page")
    db.finish_task(task_id, status="completed", summary="done", provider="groq/x", steps=3)
    rows = db.recent_tasks(limit=5, user_id=5)
    assert rows[0]["status"] == "completed"
    assert rows[0]["steps"] == 3


def test_audit_log_records_success_and_failure(db):
    db.audit("42", "command_requested", "pytest -q", ok=True)
    db.audit("42", "command_denied", "rm -rf /", ok=False)
    tail = db.audit_tail(5)
    assert len(tail) == 2
    assert tail[0]["ok"] == 0


def test_kv_cache_with_expiry(db):
    db.kv_set("models", {"gemini": ["a", "b"]})
    assert db.kv_get("models") == {"gemini": ["a", "b"]}
    assert db.kv_get("models", max_age=-1) is None
    assert db.kv_get("missing", default="fallback") == "fallback"


def test_backup_and_restore(db, settings):
    db.add_message(9, "user", "before backup")
    backup_path = db.backup(settings.backups_dir / "snap.sqlite3")
    assert backup_path.exists() and backup_path.stat().st_size > 0

    db.clear_history(9)
    assert db.history(9) == []

    db.restore(backup_path)
    assert db.history(9)[0]["content"] == "before backup"


def test_restore_refuses_foreign_file(db, tmp_path):
    bogus = tmp_path / "not-a-db.sqlite3"
    bogus.write_text("definitely not sqlite", encoding="utf-8")
    try:
        db.restore(bogus)
    except Exception as exc:  # sqlite3.DatabaseError or ValueError
        assert "not a Hermes database" in str(exc) or "file is not a database" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("restore should have refused a foreign file")


def test_stats_reports_counts(db):
    db.add_message(1, "user", "x")
    stats = db.stats()
    assert stats["messages"] == 1
    assert stats["schema_version"] == SCHEMA_VERSION
    assert stats["size_bytes"] > 0
