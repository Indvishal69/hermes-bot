"""HTTP health endpoint for Render (and any uptime monitor).

Deliberately exposes no filesystem paths, no provider models beyond booleans and
no secrets — the original ``/`` route leaked the absolute workspace path.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from flask import Flask, jsonify

from . import APP_NAME, __version__


def create_app(
    settings,
    *,
    router=None,
    db=None,
    workspace=None,
    started_at: float | None = None,
    status_hook: Callable[[], dict[str, Any]] | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    boot = started_at or time.time()

    def uptime() -> int:
        return int(time.time() - boot)

    @app.get("/health")
    def health():
        db_ok = bool(db.healthy()) if db is not None else None
        ws_ok = bool(workspace.info().get("writable")) if workspace is not None else None
        providers = router.status() if router is not None else []
        configured = sorted({row["provider"] for row in providers if row.get("configured")})
        payload = {
            "ok": bool(db_ok is not False and ws_ok is not False and configured),
            "service": APP_NAME,
            "version": __version__,
            "uptime_seconds": uptime(),
            "database": db_ok,
            "workspace_writable": ws_ok,
            "providers_configured": configured,
        }
        return jsonify(payload), (200 if payload["ok"] else 503)

    @app.get("/ready")
    def ready():
        return jsonify({"ready": True, "uptime_seconds": uptime()}), 200

    @app.get("/")
    def index():
        payload: dict[str, Any] = {
            "service": APP_NAME,
            "version": __version__,
            "status": "online",
            "uptime_seconds": uptime(),
        }
        if status_hook is not None:
            try:
                payload.update(status_hook())
            except Exception:  # noqa: BLE001 - health must never 500
                payload["status_detail"] = "unavailable"
        return jsonify(payload), 200

    return app
