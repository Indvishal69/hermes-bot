"""Agent loop: tool execution, approval gating, step limits, honest failures."""

from __future__ import annotations

import asyncio
import json

import pytest

from hermes.core.loop import AgentLoop, ApprovalStore
from hermes.db import Database
from hermes.providers.base import LLMResult, ProviderError
from hermes.tools import CommandRunner, ToolBox, Workspace


class ScriptedRouter:
    """Returns pre-scripted model replies; records the messages it saw."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.seen: list[list[dict]] = []

    def complete(self, messages, **kwargs):
        self.seen.append([dict(item) for item in messages])
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if isinstance(reply, Exception):
            raise reply
        return LLMResult(text=reply, provider="fake", model="fake-1")


def build(settings, router):
    workspace = Workspace(settings.workspace_dir, max_file_bytes=100_000)
    db = Database(settings.db_path)
    db.init()
    runner = CommandRunner(workspace.root, timeout=10)
    toolbox = ToolBox(workspace, runner, db)
    approvals = ApprovalStore(ttl=60)
    loop = AgentLoop(settings, db, router, workspace, runner, toolbox, approvals)
    return loop, workspace, db, approvals


def run(coro):
    return asyncio.run(coro)


async def collect(events):
    async def notify(kind, text):
        events.append((kind, text))

    return notify


def action(**kwargs):
    return json.dumps(kwargs)


def test_write_file_then_final(settings):
    router = ScriptedRouter(
        [
            action(action="write_file", path="site/index.html", content="<h1>Hello</h1>"),
            action(action="final", message="Created site/index.html."),
        ]
    )
    loop, workspace, db, _ = build(settings, router)
    events = []
    result = run(
        loop.handle(user_id=1, channel_id=2, prompt="make a page", notify=run(collect(events)))
    )
    assert result.status == "completed"
    assert result.steps == 2
    assert "Created site/index.html" in result.text
    assert (workspace.root / "site/index.html").read_text() == "<h1>Hello</h1>"
    # Tool result was fed back to the model, and the task is recorded.
    assert "TOOL RESULT" in router.seen[1][-1]["content"]
    assert db.recent_tasks(1)[0]["status"] == "completed"


def test_command_requires_approval_and_does_not_run(settings):
    router = ScriptedRouter([action(action="run_command", command="pytest -q", reason="run tests")])
    loop, workspace, db, approvals = build(settings, router)
    result = run(
        loop.handle(user_id=1, channel_id=2, prompt="test it", notify=run(collect([])))
    )
    assert result.status == "awaiting_approval"
    assert result.approval is not None
    assert result.approval.command == "pytest -q"
    assert approvals.pending_for(1)
    # Nothing was executed: the registry is empty.
    assert loop.runner.active() == []


def test_resume_after_approval_continues_the_task(settings):
    router = ScriptedRouter(
        [
            action(action="run_command", command="python3 -c print(1)", reason="check python"),
            action(action="final", message="Command approved and finished."),
        ]
    )
    loop, _, db, _ = build(settings, router)
    first = run(loop.handle(user_id=1, channel_id=2, prompt="go", notify=run(collect([]))))
    second = run(
        loop.resume(
            first.approval,
            command_result="exit=0\n1",
            notify=run(collect([])),
            approved=True,
        )
    )
    assert second.status == "completed"
    assert "finished" in second.text
    assert "OWNER APPROVED" in router.seen[-1][-1]["content"]


def test_blocked_command_is_not_offered_for_approval(settings):
    router = ScriptedRouter(
        [
            action(action="run_command", command="rm -rf /", reason="cleanup"),
            action(action="final", message="Could not clean up; command blocked by policy."),
        ]
    )
    loop, _, _, approvals = build(settings, router)
    result = run(loop.handle(user_id=1, channel_id=2, prompt="clean", notify=run(collect([]))))
    assert result.status == "completed"
    assert not approvals.pending_for(1)
    assert "rejected by policy" in router.seen[-1][-1]["content"]


def test_invalid_json_is_retried_once_then_recovered(settings):
    router = ScriptedRouter(
        ["I think you should create a file.", action(action="final", message="Recovered.")]
    )
    loop, _, _, _ = build(settings, router)
    result = run(loop.handle(user_id=1, channel_id=2, prompt="hi", notify=run(collect([]))))
    assert result.status == "completed"
    assert result.text == "Recovered."
    assert router.calls == 2


def test_fenced_json_is_accepted(settings):
    router = ScriptedRouter(
        ['```json\n{"action":"final","message":"fenced ok"}\n```']
    )
    loop, _, _, _ = build(settings, router)
    result = run(loop.handle(user_id=1, channel_id=2, prompt="hi", notify=run(collect([]))))
    assert result.text == "fenced ok"


def test_provider_failure_is_reported_not_hidden(settings):
    router = ScriptedRouter([ProviderError("router", "All providers failed. groq: HTTP 401")])
    loop, _, db, _ = build(settings, router)
    result = run(loop.handle(user_id=1, channel_id=2, prompt="hi", notify=run(collect([]))))
    assert result.status == "failed"
    assert "HTTP 401" in result.text
    assert db.recent_tasks(1)[0]["status"] == "failed"


def test_step_limit_produces_honest_summary(settings):
    make_settings = settings
    router = ScriptedRouter([action(action="list_files", path=".")])
    loop, _, _, _ = build(make_settings, router)
    loop.settings = type(settings)(**{**settings.__dict__, "max_agent_steps": 2})
    result = run(loop.handle(user_id=1, channel_id=2, prompt="loop forever", notify=run(collect([]))))
    assert result.status == "step_limit"
    assert "Step limit" in result.text or "step limit" in result.text.lower()


def test_workspace_escape_from_model_is_rejected(settings):
    router = ScriptedRouter(
        [
            action(action="write_file", path="../../etc/cron.d/evil", content="x"),
            action(action="final", message="Blocked."),
        ]
    )
    loop, workspace, _, _ = build(settings, router)
    result = run(loop.handle(user_id=1, channel_id=2, prompt="write outside", notify=run(collect([]))))
    assert result.status == "completed"
    assert "outside the Hermes workspace" in router.seen[-1][-1]["content"]


def test_secrets_are_redacted_from_reported_errors(settings):
    router = ScriptedRouter([ProviderError("groq", f"bad key {settings.groq_api_key}")])
    loop, _, _, _ = build(settings, router)
    result = run(loop.handle(user_id=1, channel_id=2, prompt="hi", notify=run(collect([]))))
    assert settings.groq_api_key not in result.text
    assert "[REDACTED]" in result.text


def test_conversation_history_is_used(settings):
    router = ScriptedRouter([action(action="final", message="first"), action(action="final", message="second")])
    loop, _, db, _ = build(settings, router)
    run(loop.handle(user_id=1, channel_id=2, prompt="first question", notify=run(collect([]))))
    run(loop.handle(user_id=1, channel_id=2, prompt="second question", notify=run(collect([]))))
    second_call = router.seen[1]
    roles = [item["role"] for item in second_call]
    assert roles[0] == "system"
    assert {"role": "user", "content": "first question"} in second_call
    assert {"role": "assistant", "content": "first"} in second_call


def test_memory_note_tool_persists(settings):
    router = ScriptedRouter(
        [
            action(action="memory_note", note="Project uses Flask", project="demo"),
            action(action="final", message="Noted."),
        ]
    )
    loop, _, db, _ = build(settings, router)
    run(loop.handle(user_id=1, channel_id=2, prompt="remember", notify=run(collect([])), project="demo"))
    assert db.notes("demo")[0]["note"] == "Project uses Flask"


# --------------------------------------------------------------------------
# connectors inside the agent loop
# --------------------------------------------------------------------------
from hermes.connectors.base import Connector, ConnectorError, ConnectorTool  # noqa: E402
from hermes.connectors.registry import ConnectorRegistry  # noqa: E402


class EchoConnector(Connector):
    name = "echo"
    title = "Echo"
    env_vars = ("ECHO_TOKEN",)

    def tools(self):
        return [
            ConnectorTool("echo_ping", "Ping", {}, self.ping),
            ConnectorTool("echo_write", "Write", {"text": "str"}, self.write, write=True),
        ]

    def ping(self) -> str:
        return "pong"

    def write(self, text: str = "") -> str:
        return f"wrote {text}"


def build_with_connectors(settings, router):
    workspace = Workspace(settings.workspace_dir, max_file_bytes=100_000)
    db = Database(settings.db_path)
    db.init()
    registry = ConnectorRegistry(db, env={"ECHO_TOKEN": "t"}, classes=(EchoConnector,))
    runner = CommandRunner(workspace.root, timeout=10)
    toolbox = ToolBox(workspace, runner, db, connectors=registry)
    approvals = ApprovalStore(ttl=60)
    loop = AgentLoop(settings, db, router, workspace, runner, toolbox, approvals)
    return loop, registry, db, approvals


def test_connector_read_tool_runs_without_approval(settings):
    router = ScriptedRouter([
        action(action="echo_ping"),
        action(action="final", message="pinged"),
    ])
    loop, _, _, approvals = build_with_connectors(settings, router)
    result = run(loop.handle(user_id=1, channel_id=2, prompt="ping it", notify=run(collect([]))))
    assert result.status == "completed"
    assert not approvals.pending_for(1)
    assert "pong" in router.seen[-1][-1]["content"]


def test_connector_write_requires_owner_approval(settings):
    router = ScriptedRouter([
        action(action="echo_write", text="note", reason="save a note"),
        action(action="final", message="saved"),
    ])
    loop, registry, db, approvals = build_with_connectors(settings, router)
    result = run(loop.handle(user_id=1, channel_id=2, prompt="save", notify=run(collect([]))))
    assert result.status == "awaiting_approval"
    assert result.approval.kind == "connector"
    assert result.approval.tool == "echo_write"
    assert result.approval.args == {"text": "note"}
    assert "echo_write(text=note)" in result.approval.command

    # Nothing was written before approval.
    assert db.audit_tail(1)[0]["action"] == "connector_requested"

    resumed = run(loop.resume(result.approval, command_result="wrote note",
                              notify=run(collect([])), approved=True))
    assert resumed.status == "completed"
    assert "OWNER APPROVED" in router.seen[-1][-1]["content"]


def test_disabled_connector_tools_are_not_offered(settings):
    router = ScriptedRouter([
        action(action="echo_ping"),
        action(action="final", message="cannot ping"),
    ])
    loop, registry, _, _ = build_with_connectors(settings, router)
    registry.disable("echo")
    result = run(loop.handle(user_id=1, channel_id=2, prompt="ping", notify=run(collect([]))))
    assert result.status == "completed"
    assert "Unknown action" in router.seen[-1][-1]["content"]
    assert "echo_ping" not in loop.system_prompt()


def test_prompt_lists_active_connector_tools(settings):
    router = ScriptedRouter([action(action="final", message="ok")])
    loop, _, _, _ = build_with_connectors(settings, router)
    prompt = loop.system_prompt()
    assert "echo_ping" in prompt
    assert "echo_write" in prompt
    assert "OWNER APPROVAL REQUIRED" in prompt
    assert "echo" in prompt.lower()


# --------------------------------------------------------------------------
# web tools inside the agent loop
# --------------------------------------------------------------------------
class _Page:
    status_code = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}
    encoding = "utf-8"
    _body = b"<html><title>Docs</title><body>Hermes reads this page.</body></html>"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size=1024):
        yield self._body


class _Session:
    def get(self, url, **kwargs):
        return _Page()

    def post(self, url, **kwargs):  # pragma: no cover - unused
        return _Page()


def build_with_web(settings, router):
    from hermes.browsing import WebTools
    from hermes.browsing.fetch import HttpFetcher

    workspace = Workspace(settings.workspace_dir, max_file_bytes=100_000)
    db = Database(settings.db_path)
    db.init()
    web = WebTools(settings)
    web.fetcher = HttpFetcher(session=_Session())
    runner = CommandRunner(workspace.root, timeout=10)
    toolbox = ToolBox(workspace, runner, db, webtools=web)
    loop = AgentLoop(settings, db, router, workspace, runner, toolbox, ApprovalStore(ttl=60))
    return loop, toolbox


def test_web_fetch_is_offered_and_runs(settings, public_dns):
    router = ScriptedRouter([
        action(action="web_fetch", url="https://example.com/docs"),
        action(action="final", message="Read the page."),
    ])
    loop, _ = build_with_web(settings, router)
    assert "web_fetch" in loop.system_prompt()

    result = run(loop.handle(user_id=1, channel_id=2, prompt="read docs", notify=run(collect([]))))
    assert result.status == "completed"
    fed_back = router.seen[-1][-1]["content"]
    assert "Hermes reads this page." in fed_back
    assert "untrusted data" in fed_back.lower()


def test_web_tools_absent_when_disabled(make_settings):
    router = ScriptedRouter([action(action="final", message="ok")])
    disabled = make_settings(env={"WEB_ENABLED": "false"})
    loop, toolbox = build_with_web(disabled, router)
    assert "web_fetch" not in loop.system_prompt()
    assert "web_fetch" not in toolbox.specs()


def test_web_fetch_rejects_private_hosts_inside_the_loop(settings, public_dns):
    router = ScriptedRouter([
        action(action="web_fetch", url="http://169.254.169.254/latest/meta-data/"),
        action(action="final", message="Blocked."),
    ])
    loop, _ = build_with_web(settings, router)
    result = run(loop.handle(user_id=1, channel_id=2, prompt="fetch metadata", notify=run(collect([]))))
    assert result.status == "completed"
    assert "non-public" in router.seen[-1][-1]["content"]
