"""Discord layer: attribute collisions, command dispatch, task delivery.

Regression tests for a real production failure: ``discord.Client`` owns a
``loop`` attribute and ``_async_setup_hook`` replaces it with the running asyncio
loop when the bot starts. Naming the AgentLoop ``self.loop`` therefore made
``self.loop.handle(...)`` call ``_UnixSelectorEventLoop.handle`` and every message
died with ``AttributeError: '_UnixSelectorEventLoop' object has no attribute
'handle'``. The AgentLoop is ``self.agent_loop`` now, and these tests fail if
anyone re-introduces the collision.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time

import discord
import pytest

from hermes.bot import HermesBot, StatusMessage
from hermes.core.loop import AgentRun, ApprovalStore
from hermes.db import Database
from hermes.providers import Router
from hermes.tools import CommandRunner, ToolBox, Workspace


class FakeUser:
    def __init__(self, user_id: int, bot: bool = False):
        self.id = user_id
        self.bot = bot


class FakeMessage:
    def __init__(self, channel, content: str = "", author: FakeUser | None = None):
        self.channel = channel
        self.content = content
        self.author = author or FakeUser(123456789012345678)
        self.edits: list[str] = []

    async def edit(self, content=None, **_kwargs):
        self.edits.append(content or "")
        return self


class FakeChannel:
    def __init__(self):
        self.id = 555
        self.sent: list[str] = []
        self.messages: list[FakeMessage] = []

    async def send(self, content=None, **_kwargs):
        self.sent.append(content or "")
        message = FakeMessage(self, content or "")
        self.messages.append(message)
        return message


class FakeRouter(Router):
    def __init__(self):
        super().__init__({}, [])
        self.order = ["groq"]

    def available(self):
        return ["groq"]

    def status(self):
        return [{"provider": "groq", "configured": True, "model": "fake-1",
                 "cooldown_seconds": 0, "last_error": None, "models_error": ""}]


class FakeAgentLoop:
    def __init__(self, run: AgentRun | None = None, error: Exception | None = None):
        self.run = run or AgentRun(text="Built the page.", status="completed", provider="fake/fake-1", steps=2)
        self.error = error
        self.calls: list[dict] = []
        self.resumes: list[dict] = []

    async def handle(self, **kwargs):
        self.calls.append(kwargs)
        await kwargs["notify"]("status", "step 1: write_file ok")
        if self.error:
            raise self.error
        return self.run

    async def resume(self, approval, **kwargs):
        self.resumes.append({"approval": approval, **kwargs})
        return self.run


def make_bot(settings, agent_loop=None):
    db = Database(settings.db_path)
    db.init()
    workspace = Workspace(settings.workspace_dir)
    runner = CommandRunner(workspace.root, timeout=5)
    toolbox = ToolBox(workspace, runner, db)
    return HermesBot(
        settings,
        db,
        FakeRouter(),
        workspace,
        runner,
        toolbox,
        agent_loop or FakeAgentLoop(),
        ApprovalStore(ttl=60),
        started_at=time.time(),
    )


def test_agent_loop_does_not_shadow_discord_client_attributes(settings):
    loop = FakeAgentLoop()
    bot = make_bot(settings, loop)
    assert bot.agent_loop is loop

    # Every attribute discord.Client assigns to itself must stay discord's.
    source = inspect.getsource(discord.Client.__init__) + inspect.getsource(
        discord.Client._async_setup_hook
    )
    reserved = set(re.findall(r"self\.(\w+)\s*=", source))
    ours = {name for name in vars(bot) if not name.startswith("_")}
    assert not (ours & reserved - {"loop"}), f"shadowed discord attributes: {ours & reserved}"


def test_task_survives_discord_overwriting_client_loop(settings):
    """The exact production failure: discord.py replaces bot.loop at startup."""

    async def scenario():
        loop = FakeAgentLoop()
        bot = make_bot(settings, loop)
        # This is what discord.Client._async_setup_hook does inside bot.run().
        bot.loop = asyncio.get_running_loop()
        channel = FakeChannel()
        message = FakeMessage(channel, "build me a landing page")
        await bot._handle_task(message, "build me a landing page")
        return bot, channel, loop, message

    bot, channel, loop, message = asyncio.run(scenario())

    assert loop.calls, "the agent loop was never called"
    assert loop.calls[0]["prompt"] == "build me a landing page"
    assert loop.calls[0]["user_id"] == 123456789012345678
    assert channel.sent[0] == "🧠 Hermes is working…"
    final_edit = channel.messages[0].edits[-1]
    assert "Done" in final_edit and "Built the page." in final_edit


def test_status_message_reports_progress_then_finishes(settings):
    async def scenario():
        channel = FakeChannel()
        message = await channel.send("start")
        status = StatusMessage(message, min_interval=0)
        await status.update("⚙️ step 1")
        await status.update("⚙️ step 2")
        await status.finish("✅ done")
        await status.update("ignored after finish")
        return message

    message = asyncio.run(scenario())
    assert "⚙️ step 1" in message.edits
    assert message.edits[-1] == "✅ done"
    assert "ignored after finish" not in message.edits


def test_unauthorized_user_is_ignored_and_audited(settings):
    async def scenario():
        bot = make_bot(settings)
        channel = FakeChannel()
        stranger = FakeMessage(channel, "delete everything", author=FakeUser(999, bot=False))
        await bot.on_message(stranger)
        return bot, channel

    bot, channel = asyncio.run(scenario())
    assert channel.sent == []
    assert bot.db.audit_tail(5)[0]["action"] == "unauthorized_message"


def test_bot_messages_are_ignored(settings):
    async def scenario():
        bot = make_bot(settings)
        channel = FakeChannel()
        await bot.on_message(FakeMessage(channel, "!help", author=FakeUser(123456789012345678, bot=True)))
        return channel

    assert asyncio.run(scenario()).sent == []


def test_help_command_lists_approval_flow(settings):
    async def scenario():
        bot = make_bot(settings)
        channel = FakeChannel()
        await bot.on_message(FakeMessage(channel, "!help"))
        return channel

    channel = asyncio.run(scenario())
    assert channel.sent and "!approve ID" in channel.sent[0]
    assert "!models" in channel.sent[0]


def test_unknown_command_is_treated_as_a_task(settings):
    async def scenario():
        loop = FakeAgentLoop()
        bot = make_bot(settings, loop)
        channel = FakeChannel()
        await bot.on_message(FakeMessage(channel, "!deployplease"))
        return loop

    loop = asyncio.run(scenario())
    assert loop.calls and loop.calls[0]["prompt"] == "!deployplease"


def test_task_error_is_reported_not_swallowed(settings):
    async def scenario():
        loop = FakeAgentLoop(error=RuntimeError("provider exploded"))
        bot = make_bot(settings, loop)
        channel = FakeChannel()
        await bot.on_message(FakeMessage(channel, "do something"))
        return channel

    channel = asyncio.run(scenario())
    assert any("Internal error" in text and "provider exploded" in text for text in channel.sent)


def test_rate_limit_blocks_rapid_tasks(settings):
    limited = type(settings)(**{**settings.__dict__, "task_rate_limit": 30})

    async def scenario():
        loop = FakeAgentLoop()
        bot = make_bot(limited, loop)
        channel = FakeChannel()
        await bot._handle_task(FakeMessage(channel, "first"), "first")
        await bot._handle_task(FakeMessage(channel, "second"), "second")
        return loop, channel

    loop, channel = asyncio.run(scenario())
    assert len(loop.calls) == 1
    assert any("Slow down" in text for text in channel.sent)


def test_approval_display_includes_id_and_command(settings):
    async def scenario():
        from hermes.core.loop import PendingApproval

        bot = make_bot(settings)
        channel = FakeChannel()
        approval = PendingApproval(
            id="abc12345",
            user_id=123456789012345678,
            channel_id=channel.id,
            command="pytest -q",
            reason="run the tests",
            messages=[],
            task_id=1,
        )
        run = AgentRun(text="", status="awaiting_approval", approval=approval)
        status = StatusMessage(await channel.send("working"), min_interval=0)
        await bot._deliver(channel, run, status)
        return channel

    channel = asyncio.run(scenario())
    text = channel.messages[0].edits[-1]
    assert "!approve abc12345" in text
    assert "pytest -q" in text


def test_deny_resumes_the_loop_without_running_anything(settings):
    async def scenario():
        from hermes.core.loop import PendingApproval

        loop = FakeAgentLoop()
        bot = make_bot(settings, loop)
        approval = bot.approvals.add(
            user_id=123456789012345678,
            channel_id=555,
            command="pytest -q",
            reason="tests",
            messages=[],
            task_id=1,
            steps=1,
        )
        channel = FakeChannel()
        await bot._cmd_approval(FakeMessage(channel, "!deny abc"), "!deny", approval.id)
        return loop, channel

    loop, channel = asyncio.run(scenario())
    assert loop.resumes and loop.resumes[0]["approved"] is False
    assert any("Denied" in text for text in channel.sent)
