#!/usr/bin/env python3
"""Hermes 3.0 entry point.

Starts:
* the Discord bot (owner-only),
* the Flask health server on ``$PORT`` (Render health checks),
* SQLite with migrations, and the workspace/command sandbox.

Run modes:
    python agent.py            # normal service
    python agent.py --check    # validate configuration and storage, then exit
    python agent.py --version
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from hermes import APP_NAME, __version__
from hermes.bot import HermesBot
from hermes.browsing import WebTools
from hermes.config import ensure_storage, load_settings
from hermes.connectors import ConnectorRegistry
from hermes.core import AgentLoop, ApprovalStore
from hermes.db import Database
from hermes.providers import build_router
from hermes.tools import CommandRunner, ToolBox, Workspace
from hermes.web import create_app

log = logging.getLogger("hermes")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def preflight(settings, *, verbose: bool = True) -> tuple[bool, list[str]]:
    """Check configuration and storage without touching Discord."""
    notes: list[str] = []
    ok = True

    for problem in settings.problems:
        notes.append(f"config: {problem}")
        ok = False

    for folder in (settings.data_dir, settings.workspace_dir, settings.backups_dir):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".hermes-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            notes.append(f"storage: {folder} writable")
        except OSError as exc:
            notes.append(f"storage: {folder} NOT writable ({type(exc).__name__}: {exc})")
            ok = False

    db = Database(settings.db_path)
    try:
        version = db.init()
        notes.append(f"database: {settings.db_path} ready (schema v{version})")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"database: FAILED ({type(exc).__name__}: {exc})")
        ok = False

    for name, present in settings.providers_configured.items():
        notes.append(f"provider {name}: {'configured' if present else 'not configured'}")

    try:
        from hermes.connectors import ConnectorRegistry

        registry = ConnectorRegistry(db)
        configured = [row["name"] for row in registry.status() if row["configured"]]
        notes.append(
            f"connectors: {registry.active_count()} active of {len(registry.names())}"
            + (f" (configured: {', '.join(configured)})" if configured else "")
        )
    except Exception as exc:  # noqa: BLE001 - preflight must not fail on this
        notes.append(f"connectors: could not be inspected ({type(exc).__name__})")

    if settings.web_enabled:
        search = settings.search_provider or "not configured"
        notes.append(
            f"web: fetch on, search {search}, "
            f"browser {'on' if settings.browser_enabled else 'off'}"
        )
    else:
        notes.append("web: disabled (WEB_ENABLED=false)")

    if verbose:
        for line in notes:
            print(f"  {line}")
    return ok, notes


def build_runtime(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)

    db = Database(settings.db_path)
    db.init()

    workspace = Workspace(
        settings.workspace_dir,
        max_file_bytes=settings.max_file_bytes,
        max_output_chars=settings.max_output_chars,
    )
    runner = CommandRunner(
        settings.workspace_dir,
        timeout=settings.command_timeout,
        max_output_chars=settings.max_output_chars,
        allow_git_push=settings.allow_git_push,
    )
    connectors = ConnectorRegistry(
        db,
        secrets=settings.secrets,
        max_chars=settings.max_output_chars,
        workspace=workspace,
        youtube_max_upload_mb=settings.youtube_max_upload_mb,
    )
    webtools = WebTools(settings)
    toolbox = ToolBox(
        workspace,
        runner,
        db,
        max_output_chars=settings.max_output_chars,
        connectors=connectors,
        webtools=webtools,
    )
    router = build_router(settings)
    approvals = ApprovalStore(ttl=settings.approval_ttl)
    loop = AgentLoop(
        settings, db, router, workspace, runner, toolbox, approvals, app_name=APP_NAME
    )
    return db, workspace, runner, toolbox, router, approvals, loop, connectors, webtools


def start_health_server(
    settings, db, workspace, router, started_at: float, connectors=None
) -> threading.Thread:
    def status_hook():
        stats = db.stats()
        return {
            "database_schema": stats["schema_version"],
            "workspace_files": workspace.info()["files"],
            "providers": [row["provider"] for row in router.status() if row["configured"]],
            "connectors_active": connectors.active_count() if connectors else 0,
        }

    app = create_app(
        settings,
        router=router,
        db=db,
        workspace=workspace,
        started_at=started_at,
        status_hook=status_hook,
        connectors=connectors,
    )

    def serve() -> None:
        try:
            app.run(
                host="0.0.0.0",
                port=settings.port,
                threaded=True,
                use_reloader=False,
                debug=False,
            )
        except OSError as exc:
            log.error("Health server could not bind port %s: %s", settings.port, exc)

    thread = threading.Thread(target=serve, daemon=True, name="hermes-health")
    thread.start()
    log.info("Health server listening on 0.0.0.0:%s (/health)", settings.port)
    return thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} {__version__}")
    parser.add_argument("--check", action="store_true", help="validate config and storage, then exit")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args(argv)

    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0

    settings = load_settings()
    configure_logging(settings.log_level)

    # A missing/unwritable DATA_DIR must not take the bot offline.
    settings, storage_warnings = ensure_storage(settings)
    for warning in storage_warnings:
        log.warning("%s", warning)

    if args.check:
        print(f"{APP_NAME} {__version__} — preflight")
        ok, _ = preflight(settings)
        print("RESULT:", "OK" if ok else "PROBLEMS FOUND")
        return 0 if ok else 1

    ok, _ = preflight(settings, verbose=False)
    if not ok:
        log.error("Refusing to start — configuration or storage problems:")
        for problem in settings.problems:
            log.error("  • %s", problem)
        return 1

    started_at = time.time()
    log.info("Starting %s %s", APP_NAME, __version__)
    log.info("Settings: %s", settings.redacted_summary())

    try:
        db, workspace, runner, toolbox, router, approvals, loop, connectors, webtools = build_runtime(settings)
    except Exception as exc:  # noqa: BLE001
        log.exception("Startup failed while initialising runtime: %s", exc)
        return 1

    log.info(
        "Connectors: %s active of %s",
        connectors.active_count(),
        len(connectors.names()),
    )
    start_health_server(settings, db, workspace, router, started_at, connectors)

    bot = HermesBot(
        settings,
        db,
        router,
        workspace,
        runner,
        toolbox,
        loop,
        approvals,
        started_at=started_at,
        connectors=connectors,
        webtools=webtools,
    )

    def shutdown(signum, _frame):
        log.warning("Received signal %s — shutting down", signum)
        try:
            db.audit("system", "shutdown", f"signal {signum}")
        finally:
            raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    try:
        bot.run(settings.discord_token, log_handler=None)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception as exc:  # noqa: BLE001
        log.exception("Bot stopped unexpectedly: %s", exc)
        return 1
    finally:
        try:
            db.audit("system", "stop", "process exited")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
