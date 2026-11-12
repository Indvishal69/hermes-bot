"""SQLite persistence: chat memory, project memory, task history, audit log.

Uses plain ``sqlite3`` (no ORM) with a small forward-only migration runner so
schema changes are safe and repeatable on an existing database.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# (version, statements). Never edit an applied migration; add a new one.
MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE IF NOT EXISTS chat_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_memory(user_id, id)",
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                summary TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                steps INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, id DESC)",
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                ok INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(created_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
        ),
    ),
    (
        2,
        (
            """
            CREATE TABLE IF NOT EXISTS project_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_project ON project_memory(project, id DESC)",
        ),
    ),
)

SCHEMA_VERSION = max(version for version, _ in MIGRATIONS)


class Database:
    """Thin, thread-safe-enough wrapper around a single SQLite file."""

    def __init__(self, path: Path, *, max_messages_per_user: int = 120):
        self.path = Path(path)
        self.max_messages_per_user = max_messages_per_user

    # -- plumbing -----------------------------------------------------------
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=20000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> int:
        """Apply pending migrations. Returns the resulting schema version."""
        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, statements in MIGRATIONS:
                if version in applied:
                    continue
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(?,?)",
                    (version, time.time()),
                )
        return self.schema_version()

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row["v"] or 0) if row else 0

    def healthy(self) -> bool:
        try:
            with self.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except (sqlite3.Error, OSError):
            return False

    # -- chat memory --------------------------------------------------------
    def add_message(self, user_id: int | str, role: str, content: str, *, limit: int = 12_000) -> None:
        uid = str(user_id)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO chat_memory(user_id, role, content, created_at) VALUES(?,?,?,?)",
                (uid, role, (content or "")[:limit], time.time()),
            )
            conn.execute(
                """
                DELETE FROM chat_memory
                WHERE user_id = ? AND id NOT IN (
                    SELECT id FROM chat_memory WHERE user_id = ?
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (uid, uid, self.max_messages_per_user),
            )

    def history(self, user_id: int | str, limit: int = 20) -> list[dict[str, str]]:
        if limit <= 0:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM chat_memory WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (str(user_id), int(limit)),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def clear_history(self, user_id: int | str) -> int:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM chat_memory WHERE user_id=?", (str(user_id),))
            return cur.rowcount or 0

    # -- project memory -----------------------------------------------------
    def add_note(self, project: str, note: str, *, keep: int = 200) -> None:
        project = (project or "default")[:64]
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO project_memory(project, note, created_at) VALUES(?,?,?)",
                (project, note[:4_000], time.time()),
            )
            conn.execute(
                """
                DELETE FROM project_memory
                WHERE project = ? AND id NOT IN (
                    SELECT id FROM project_memory WHERE project = ? ORDER BY id DESC LIMIT ?
                )
                """,
                (project, project, keep),
            )

    def notes(self, project: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if project:
                rows = conn.execute(
                    "SELECT project, note, created_at FROM project_memory "
                    "WHERE project=? ORDER BY id DESC LIMIT ?",
                    (project[:64], limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT project, note, created_at FROM project_memory "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def clear_notes(self, project: str | None = None) -> int:
        with self.connect() as conn:
            if project:
                cur = conn.execute("DELETE FROM project_memory WHERE project=?", (project[:64],))
            else:
                cur = conn.execute("DELETE FROM project_memory")
            return cur.rowcount or 0

    # -- tasks --------------------------------------------------------------
    def start_task(self, user_id: int | str, prompt: str) -> int:
        now = time.time()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO tasks(user_id, prompt, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?)",
                (str(user_id), prompt[:4_000], "running", now, now),
            )
            return int(cur.lastrowid)

    def finish_task(
        self,
        task_id: int,
        *,
        status: str,
        summary: str = "",
        provider: str = "",
        steps: int = 0,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, summary=?, provider=?, steps=?, updated_at=? WHERE id=?",
                (status, summary[:2_000], provider[:120], int(steps), time.time(), int(task_id)),
            )

    def recent_tasks(self, limit: int = 10, user_id: int | str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if user_id is None:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE user_id=? ORDER BY id DESC LIMIT ?",
                    (str(user_id), limit),
                ).fetchall()
        return [dict(row) for row in rows]

    # -- audit --------------------------------------------------------------
    def audit(self, actor: str, action: str, detail: str = "", ok: bool = True) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(actor, action, detail, ok, created_at) VALUES(?,?,?,?,?)",
                (str(actor)[:64], str(action)[:64], (detail or "")[:4_000], 1 if ok else 0, time.time()),
            )

    def audit_tail(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # -- kv cache -----------------------------------------------------------
    def kv_set(self, key: str, value: Any) -> None:
        payload = value if isinstance(value, str) else json.dumps(value)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, payload, time.time()),
            )

    def kv_get(self, key: str, default: Any = None, *, max_age: float | None = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value, updated_at FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        if max_age is not None and time.time() - row["updated_at"] > max_age:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    # -- stats / maintenance ------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            def count(table: str) -> int:
                try:
                    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
                except sqlite3.Error:
                    return 0

            messages = count("chat_memory")
            tasks = count("tasks")
            notes = count("project_memory")
            audits = count("audit_log")
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path": str(self.path),
            "schema_version": self.schema_version(),
            "messages": messages,
            "tasks": tasks,
            "notes": notes,
            "audit_rows": audits,
            "size_bytes": size,
        }

    def backup(self, destination: Path) -> Path:
        """Consistent online backup using SQLite's backup API."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as src, sqlite3.connect(destination) as dst:
            src.backup(dst)
        return destination

    def restore(self, source: Path) -> None:
        """Replace the live database with a backup (validated first)."""
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(f"Backup not found: {source}")
        with sqlite3.connect(source) as check:
            tables = {
                row[0]
                for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        if "schema_migrations" not in tables:
            raise ValueError("Refusing to restore: file is not a Hermes database.")
        if self.path.exists():
            self.backup(self.path.with_suffix(".pre-restore.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, self.path)
