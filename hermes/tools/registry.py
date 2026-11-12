"""Tool registry: the single place that defines what the model may ask for.

The model is instructed to return one JSON object per step. Every object is
validated here before anything touches the filesystem or a process, so a
hallucinated or hostile action cannot do damage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..connectors.base import ConnectorError
from ..db import Database
from ..utils import truncate
from .files import Workspace, WorkspaceError
from .shell import CommandRejected, CommandRunner

log = logging.getLogger("hermes.tools")

# Compact schema shown to the model. Kept declarative so the prompt and the
# dispatcher can never drift apart.
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "list_files": {
        "args": {"path": "str (optional, default '.')"},
        "desc": "List files and folders in a workspace directory.",
        "approval": False,
    },
    "tree": {
        "args": {"path": "str (optional)", "max_depth": "int (optional, default 2)"},
        "desc": "Show a shallow directory tree.",
        "approval": False,
    },
    "read_file": {
        "args": {
            "path": "str (required)",
            "start_line": "int (optional, default 1)",
            "max_lines": "int (optional, 0 = all)",
        },
        "desc": "Read a UTF-8 text file from the workspace.",
        "approval": False,
    },
    "write_file": {
        "args": {"path": "str (required)", "content": "str (required, complete file)"},
        "desc": "Create or overwrite a file with complete content.",
        "approval": False,
    },
    "replace_in_file": {
        "args": {
            "path": "str (required)",
            "old": "str (required, exact text)",
            "new": "str (required)",
            "count": "int (optional, default 1; 0 = all)",
        },
        "desc": "Replace exact text in an existing file (safer than rewriting it).",
        "approval": False,
    },
    "append_file": {
        "args": {"path": "str (required)", "content": "str (required)"},
        "desc": "Append text to a file, creating it if missing.",
        "approval": False,
    },
    "delete_file": {
        "args": {"path": "str (required)"},
        "desc": "Delete one file or an empty directory.",
        "approval": False,
    },
    "search_files": {
        "args": {
            "pattern": "str (required, regex)",
            "glob": "str (optional, e.g. '*.py')",
            "max_results": "int (optional, default 50)",
        },
        "desc": "Search file contents inside the workspace.",
        "approval": False,
    },
    "make_zip": {
        "args": {
            "name": "str (required, e.g. 'build.zip')",
            "paths": "list[str] (optional; default = whole workspace)",
        },
        "desc": "Create a ZIP archive inside the workspace.",
        "approval": False,
    },
    "check_python": {
        "args": {"paths": "list[str] (required)"},
        "desc": "Syntax-check Python files without running them.",
        "approval": False,
    },
    "list_processes": {
        "args": {},
        "desc": "Show commands this agent has run and whether they are still running.",
        "approval": False,
    },
    "memory_note": {
        "args": {"note": "str (required)", "project": "str (optional, default 'default')"},
        "desc": "Save a durable project note to memory.",
        "approval": False,
    },
    "run_command": {
        "args": {
            "command": "str (required, single command, no shell operators)",
            "reason": "str (required, why it is needed)",
        },
        "desc": "Request a shell command. The owner must approve it in Discord before it runs.",
        "approval": True,
    },
    "final": {
        "args": {"message": "str (required, the user-facing answer)"},
        "desc": "Finish the task and answer the owner.",
        "approval": False,
    },
}


def tool_prompt_block(extra: dict[str, dict[str, Any]] | None = None) -> str:
    """Render the tool list as instructions for the model.

    ``extra`` carries connector tools, which are only present while their
    connector is configured *and* enabled.
    """
    lines = ["Available tools — reply with exactly ONE JSON object per step:", ""]
    specs = dict(TOOL_SPECS)
    if extra:
        specs.update(extra)
    for name, spec in specs.items():
        args = ", ".join(f'"{key}": {value}' for key, value in spec["args"].items())
        gate = " [OWNER APPROVAL REQUIRED]" if spec["approval"] else ""
        lines.append(f'- {{"action":"{name}"{", " + args if args else ""}}}{gate}')
        lines.append(f"  {spec['desc']}")
    return "\n".join(lines)


@dataclass
class ToolResult:
    ok: bool
    text: str
    action: str
    requires_approval: bool = False
    approval: dict[str, Any] | None = None
    data: dict[str, Any] = field(default_factory=dict)


class ToolBox:
    """Executes validated tool calls against the workspace."""

    def __init__(
        self,
        workspace: Workspace,
        runner: CommandRunner,
        db: Database,
        *,
        max_output_chars: int = 7_000,
        connectors: Any | None = None,
        webtools: Any | None = None,
    ):
        self.workspace = workspace
        self.runner = runner
        self.db = db
        self.max_output_chars = max_output_chars
        self.connectors = connectors
        self.webtools = webtools

    # -- specs --------------------------------------------------------------
    def connector_specs(self) -> dict[str, dict[str, Any]]:
        if self.connectors is None:
            return {}
        return self.connectors.tool_specs()

    def web_specs(self) -> dict[str, dict[str, Any]]:
        if self.webtools is None:
            return {}
        return self.webtools.specs()

    def specs(self) -> dict[str, dict[str, Any]]:
        merged = dict(TOOL_SPECS)
        merged.update(self.web_specs())
        merged.update(self.connector_specs())
        return merged

    def execute(self, action: dict[str, Any], *, project: str = "default") -> ToolResult:
        name = str(action.get("action", "")).strip()

        # Web tools are dynamic: only offered when they can work.
        if self.webtools is not None and name in self.webtools.specs():
            return self._execute_web(name, action)

        # Connector tools are dynamic: only offered while active.
        if self.connectors is not None and name in self.connectors.active_tools():
            return self._execute_connector(name, action)

        spec = TOOL_SPECS.get(name)
        if spec is None:
            return ToolResult(
                ok=False,
                action=name or "(missing)",
                text=f"Unknown action '{name}'. Use one of: {', '.join(TOOL_SPECS)}.",
            )
        if spec["approval"]:
            # Approval is handled by the agent loop; nothing runs here.
            return ToolResult(
                ok=True,
                action=name,
                text="approval required",
                requires_approval=True,
                approval={
                    "command": str(action.get("command", "")).strip(),
                    "reason": str(action.get("reason", "")).strip() or "No reason supplied",
                },
            )
        try:
            text = self._dispatch(name, action, project=project)
            return ToolResult(ok=True, action=name, text=truncate(text, self.max_output_chars))
        except (WorkspaceError, CommandRejected) as exc:
            return ToolResult(ok=False, action=name, text=f"{name} rejected: {exc}")
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, action=name, text=f"{name} failed: {type(exc).__name__}: {exc}")

    def _execute_web(self, name: str, action: dict[str, Any]) -> ToolResult:
        args = {key: value for key, value in action.items() if key != "action"}
        try:
            text = self.webtools.execute(name, args)
            return ToolResult(ok=True, action=name, text=truncate(text, self.max_output_chars))
        except Exception as exc:  # noqa: BLE001 - report, never raise into the loop
            return ToolResult(ok=False, action=name, text=f"{name} failed: {exc}")

    def _execute_connector(self, name: str, action: dict[str, Any]) -> ToolResult:
        # "reason" explains the call to the owner; it is not a tool argument.
        args = {
            key: value
            for key, value in action.items()
            if key not in ("action", "reason")
        }
        try:
            summary = self.connectors.summary(name, args)
        except Exception:  # noqa: BLE001 - summary must never block execution
            summary = name
        if self.connectors.needs_approval(name):
            reason = str(action.get("reason", "") or "").strip() or (
                f"{name} changes data on an external service"
            )
            return ToolResult(
                ok=True,
                action=name,
                text="approval required",
                requires_approval=True,
                approval={
                    "kind": "connector",
                    "tool": name,
                    "args": args,
                    "summary": summary,
                    "reason": reason,
                },
            )
        try:
            text = self.connectors.call(name, args)
            return ToolResult(
                ok=True, action=name, text=truncate(text, self.max_output_chars)
            )
        except ConnectorError as exc:
            return ToolResult(ok=False, action=name, text=f"{name} failed: {exc}")
        except (OSError, ValueError) as exc:
            return ToolResult(
                ok=False, action=name, text=f"{name} failed: {type(exc).__name__}: {exc}"
            )

    # -- individual tools ---------------------------------------------------
    def _dispatch(self, name: str, action: dict[str, Any], *, project: str) -> str:
        if name == "list_files":
            return self.workspace.list_files(str(action.get("path", ".")))
        if name == "tree":
            return self.workspace.tree(
                str(action.get("path", ".")),
                max_depth=int(action.get("max_depth", 2) or 2),
            )
        if name == "read_file":
            return self.workspace.read_text(
                str(action.get("path", "")),
                start_line=int(action.get("start_line", 1) or 1),
                max_lines=int(action.get("max_lines", 0) or 0),
            )
        if name == "write_file":
            return self.workspace.write_text(
                str(action.get("path", "")), str(action.get("content", ""))
            )
        if name == "replace_in_file":
            return self.workspace.replace_in_file(
                str(action.get("path", "")),
                str(action.get("old", "")),
                str(action.get("new", "")),
                count=int(action.get("count", 1) or 0),
            )
        if name == "append_file":
            return self.workspace.append_text(
                str(action.get("path", "")), str(action.get("content", ""))
            )
        if name == "delete_file":
            return self.workspace.delete(str(action.get("path", "")))
        if name == "search_files":
            return self.workspace.search(
                str(action.get("pattern", "")),
                glob=str(action.get("glob", "*") or "*"),
                max_results=int(action.get("max_results", 50) or 50),
            )
        if name == "make_zip":
            paths = action.get("paths")
            if isinstance(paths, str):
                paths = [paths]
            return self.workspace.make_zip(str(action.get("name", "archive.zip")), paths)
        if name == "check_python":
            paths = action.get("paths") or []
            if isinstance(paths, str):
                paths = [paths]
            return self.runner.python_syntax_check([str(p) for p in paths])
        if name == "list_processes":
            rows = self.runner.active()
            if not rows:
                return "No commands have been run in this session."
            return "\n".join(
                f"pid {row['pid']} {'running' if row['running'] else 'finished'} "
                f"{row['seconds']}s exit={row['exit_code']} :: {row['command']}"
                for row in rows
            )
        if name == "memory_note":
            note = str(action.get("note", "")).strip()
            if not note:
                raise ValueError("note must not be empty")
            self.db.add_note(str(action.get("project", project) or project), note)
            return "Saved to project memory."
        raise ValueError(f"Unhandled action {name}")
