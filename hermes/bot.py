"""Discord interface: owner-only commands and the task runner."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any

import discord

from . import APP_NAME, __version__
from .config import Settings
from .connectors import ConnectorError, ConnectorRegistry
from .core import AgentLoop, AgentRun, ApprovalStore, PendingApproval
from .db import Database
from .providers import ProviderError, Router
from .tools import CommandRunner, ToolBox, Workspace
from .utils import chunk_text, human_bytes, human_seconds, redact, truncate

log = logging.getLogger("hermes.bot")

HELP_TEXT = """**{app} — commands**
`!help` — this list
`!health` — database, workspace, provider health
`!status` — uptime, task counts, provider state
`!model` — show active provider + models
`!model groq|gemini` — switch preferred provider (this process)
`!models` — list models available to your API keys
`!workspace` — directory tree of the workspace
`!memory` — memory stats; `!memory <note>` saves a note; `!memory clear` wipes notes
`!reset` — clear your conversation history
`!tasks` — recent task history
`!approve ID` / `!deny ID` — approve or reject a pending command
`!backup` — snapshot the SQLite database
`!testai` — test each provider independently and show real errors
`!connectors` — list connectors (connected / configured / missing)
`!connect NAME` / `!disconnect NAME` — enable or disable a connector (saved)
`!connector NAME` — detail; `!connector NAME test` — live check;
`!connector NAME set key=value` — save a non-secret setting
`!restart` — restart the bot process

Send any other message to give Hermes a task."""


class StatusMessage:
    """Progress message with throttled edits (Discord rate limits)."""

    def __init__(self, message: discord.Message, min_interval: float = 2.0):
        self.message = message
        self.min_interval = min_interval
        self._last = 0.0
        self._last_text = ""
        self._closed = False

    async def update(self, text: str, *, force: bool = False) -> None:
        if self._closed:
            return
        now = time.time()
        if not force and now - self._last < self.min_interval:
            return
        if text == self._last_text:
            return
        try:
            await self.message.edit(content=truncate(text, 1_900))
            self._last = now
            self._last_text = text
        except discord.HTTPException:
            self._closed = True

    async def finish(self, text: str) -> None:
        self._closed = False
        self._last = 0.0
        await self.update(text, force=True)
        self._closed = True


class HermesBot(discord.Client):
    def __init__(
        self,
        settings: Settings,
        db: Database,
        router: Router,
        workspace: Workspace,
        runner: CommandRunner,
        toolbox: ToolBox,
        loop: AgentLoop,
        approvals: ApprovalStore,
        *,
        started_at: float,
        connectors: ConnectorRegistry | None = None,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.db = db
        self.router = router
        self.workspace = workspace
        self.runner = runner
        self.toolbox = toolbox
        # NOTE: never name this ``self.loop``. ``discord.Client`` owns a ``loop``
        # attribute and ``_async_setup_hook`` overwrites it with the running
        # asyncio loop at startup, which turned ``self.loop.handle(...)`` into
        # ``_UnixSelectorEventLoop.handle`` -> AttributeError on every message.
        self.agent_loop = loop
        self.approvals = approvals
        self.connectors = connectors
        self.started_at = started_at
        self._task_lock = asyncio.Lock()
        self._last_task: dict[int, float] = {}
        self._tasks_run = 0

    # -- helpers ------------------------------------------------------------
    def _clean(self, text: Any) -> str:
        return redact(truncate(text, 1_800), self.settings.secrets)

    @property
    def uptime(self) -> str:
        return human_seconds(time.time() - self.started_at)

    async def _send(self, channel: Any, text: str) -> None:
        for chunk in chunk_text(self._clean(text), 1_900):
            await channel.send(chunk)

    # -- lifecycle ----------------------------------------------------------
    async def on_ready(self) -> None:
        log.info("%s %s online as %s", APP_NAME, __version__, self.user)
        log.info("Workspace: %s", self.workspace.root)
        log.info("Providers: %s", ", ".join(self.router.available()) or "none")
        await self._resolve_models()
        try:
            await self.change_presence(
                activity=discord.Activity(type=discord.ActivityType.watching, name="the workspace")
            )
        except discord.HTTPException:
            pass

    async def _resolve_models(self) -> None:
        """Check each provider's model ID at startup so ``!health`` is accurate.

        A retired ID (Groq retires models on a schedule) would otherwise only be
        discovered on the first failed request.
        """
        for name in list(self.router.order):
            provider = self.router.providers.get(name)
            if provider is None or not provider.configured():
                continue
            resolver = getattr(provider, "resolve_model", None)
            if resolver is None:
                continue
            try:
                chosen = await asyncio.to_thread(resolver)
                reason = getattr(provider, "selection_reason", lambda: "")()
                log.info("%s model resolved: %s (%s)", name, chosen, reason or "ok")
            except ProviderError as exc:
                log.warning("%s model resolution failed: %s", name, exc.detail)
            except Exception as exc:  # noqa: BLE001 - startup must never crash
                log.warning("%s model resolution error: %s: %s", name, type(exc).__name__, exc)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.author.id != self.settings.allowed_user_id:
            self.db.audit(message.author.id, "unauthorized_message", message.content[:200], ok=False)
            log.warning("Ignored message from unauthorized user %s", message.author.id)
            return

        text = (message.content or "").strip()
        if not text:
            return

        try:
            if text.startswith("!"):
                handled = await self._handle_command(message, text)
                if handled:
                    return
            await self._handle_task(message, text)
        except discord.HTTPException as exc:
            log.warning("Discord API error: %s", exc)
        except Exception as exc:  # noqa: BLE001 - a bad message must not kill the bot
            log.exception("Unhandled error while processing a message")
            try:
                await message.channel.send(
                    f"❌ Internal error: {type(exc).__name__}: {self._clean(exc)}"
                )
            except discord.HTTPException:
                pass

    # -- commands -----------------------------------------------------------
    async def _handle_command(self, message: discord.Message, text: str) -> bool:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command == "!help":
            await message.channel.send(HELP_TEXT.format(app=APP_NAME))
            return True
        if command == "!health":
            await self._send(message.channel, self._health_text())
            return True
        if command == "!status":
            await self._send(message.channel, self._status_text())
            return True
        if command == "!model":
            await self._cmd_model(message, arg)
            return True
        if command == "!models":
            await self._cmd_models(message)
            return True
        if command == "!workspace":
            tree = await asyncio.to_thread(
                self.workspace.tree, ".", max_depth=3, max_entries=120
            )
            await self._send(message.channel, "```text\n" + tree + "\n```")
            return True
        if command == "!memory":
            await self._cmd_memory(message, arg)
            return True
        if command == "!reset":
            removed = self.db.clear_history(message.author.id)
            await message.channel.send(f"🧹 Cleared {removed} stored message(s) of conversation history.")
            return True
        if command == "!tasks":
            await self._cmd_tasks(message)
            return True
        if command in ("!approve", "!deny"):
            await self._cmd_approval(message, command, arg)
            return True
        if command == "!backup":
            await self._cmd_backup(message)
            return True
        if command == "!testai":
            await self._cmd_testai(message)
            return True
        if command in ("!connectors", "!integrations"):
            await self._send(message.channel, self._connectors_text())
            return True
        if command in ("!connect", "!disconnect"):
            await self._cmd_connect(message, command, arg)
            return True
        if command == "!connector":
            await self._cmd_connector(message, arg)
            return True
        if command == "!restart":
            await message.channel.send("♻️ Restarting the Hermes process now.")
            self.db.audit(message.author.id, "restart", "owner requested restart")
            await self.close()
            os.execv(sys.executable, [sys.executable] + sys.argv)
            return True
        return False  # not a known command: treat as a task

    def _health_text(self) -> str:
        db_stats = self.db.stats()
        ws = self.workspace.info()
        providers = self.router.status()
        lines = [
            f"**{APP_NAME} {__version__}** — online for {self.uptime}",
            f"Database: {'ok' if self.db.healthy() else 'UNHEALTHY'} "
            f"(schema v{db_stats['schema_version']}, {human_bytes(db_stats['size_bytes'])})",
            f"Workspace: {ws['files']} file(s), {human_bytes(ws['size_bytes'])}, "
            f"{'writable' if ws['writable'] else 'READ-ONLY'}",
            "Providers:",
        ]
        for row in providers:
            state = "configured" if row["configured"] else "not configured"
            extra = f", cooling down {row['cooldown_seconds']}s" if row.get("cooldown_seconds") else ""
            lines.append(f"  • {row['provider']}: {state}, model `{row.get('model') or '-'}`{extra}")
            if row.get("last_error"):
                lines.append(f"    last error: {truncate(row['last_error'].get('message', ''), 220)}")
            if row.get("models_error"):
                lines.append(f"    model list: {truncate(row['models_error'], 200)}")
        return "\n".join(lines)

    def _status_text(self) -> str:
        stats = self.db.stats()
        pending = self.approvals.pending_for(self.settings.allowed_user_id)
        lines = [
            f"**Status** — uptime {self.uptime}",
            f"Tasks run this session: {self._tasks_run}",
            f"Tasks stored: {stats['tasks']} | messages: {stats['messages']} | notes: {stats['notes']}",
            f"Connectors connected: {self.connectors.active_count() if self.connectors else 0}"
            f" / {len(self.connectors.names()) if self.connectors else 0}",
            f"Pending approvals: {len(pending)}"
            + (f" ({', '.join(item.id for item in pending)})" if pending else ""),
            f"Preferred provider: `{self.settings.active_provider}`",
            f"Step budget: {self.settings.max_agent_steps} | command timeout: {self.settings.command_timeout}s",
            f"git push allowed: {self.settings.allow_git_push}",
        ]
        active = self.runner.active()[:5]
        if active:
            lines.append("Recent commands:")
            for row in active:
                lines.append(
                    f"  • pid {row['pid']} {'running' if row['running'] else 'done'} "
                    f"{row['seconds']}s — {truncate(row['command'], 60)}"
                )
        return "\n".join(lines)

    async def _cmd_model(self, message: discord.Message, arg: str) -> None:
        if not arg:
            lines = [f"Preferred provider: `{self.settings.active_provider}`"]
            for row in self.router.status():
                lines.append(
                    f"• {row['provider']}: `{row.get('model') or '-'}` "
                    f"({'configured' if row['configured'] else 'not configured'})"
                )
            lines.append("Switch with `!model groq` or `!model gemini`.")
            await self._send(message.channel, "\n".join(lines))
            return
        choice = arg.lower()
        if choice not in ("groq", "gemini"):
            await message.channel.send("Use `!model groq` or `!model gemini`.")
            return
        if choice not in self.router.available():
            await message.channel.send(f"`{choice}` is not configured (missing API key).")
            return
        self.router.order.remove(choice)
        self.router.order.insert(0, choice)
        self.router.reset(choice)
        await message.channel.send(f"✅ Preferred provider is now `{choice}` (this process only).")

    async def _cmd_models(self, message: discord.Message) -> None:
        status = await message.channel.send("🔍 Listing models available to your API keys…")
        models = await asyncio.to_thread(self.router.all_models, refresh=True)
        lines = []
        for provider, names in models.items():
            if not names:
                row = next((r for r in self.router.status() if r["provider"] == provider), {})
                reason = row.get("models_error") or "not configured or listing failed"
                lines.append(f"**{provider}**: none — {truncate(str(reason), 300)}")
                continue
            shown = names[:40]
            lines.append(f"**{provider}** ({len(names)} text models):")
            lines.extend(f"  • `{name}`" for name in shown)
            if len(names) > len(shown):
                lines.append(f"  …and {len(names) - len(shown)} more")
        await status.edit(content=truncate(self._clean("\n".join(lines) or "No models found."), 1_900))

    async def _cmd_memory(self, message: discord.Message, arg: str) -> None:
        if arg.lower() == "clear":
            removed = self.db.clear_notes()
            await message.channel.send(f"🧹 Cleared {removed} project note(s).")
            return
        if arg:
            self.db.add_note("default", arg)
            await message.channel.send("📝 Saved to project memory.")
            return
        stats = self.db.stats()
        notes = self.db.notes(limit=8)
        lines = [
            "**Memory**",
            f"Schema v{stats['schema_version']} at `{self.settings.db_path.name}` "
            f"({human_bytes(stats['size_bytes'])})",
            f"Conversation messages: {stats['messages']} (max {self.db.max_messages_per_user}/user)",
            f"Tasks: {stats['tasks']} | audit rows: {stats['audit_rows']}",
        ]
        if notes:
            lines.append("Recent project notes:")
            lines.extend(f"  • [{row['project']}] {truncate(row['note'], 160)}" for row in notes)
        else:
            lines.append("No project notes yet. Add one with `!memory <text>`.")
        await self._send(message.channel, "\n".join(lines))

    async def _cmd_tasks(self, message: discord.Message) -> None:
        rows = self.db.recent_tasks(limit=8, user_id=message.author.id)
        if not rows:
            await message.channel.send("No tasks recorded yet.")
            return
        lines = ["**Recent tasks**"]
        for row in rows:
            when = time.strftime("%d/%m %H:%M", time.gmtime(row["created_at"]))
            lines.append(
                f"`#{row['id']}` {when} UTC — {row['status']} via {row['provider'] or '-'} "
                f"({row['steps']} steps): {truncate(row['prompt'], 90)}"
            )
        await self._send(message.channel, "\n".join(lines))

    async def _cmd_backup(self, message: discord.Message) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        target = self.settings.backups_dir / f"hermes-{stamp}.sqlite3"
        path = await asyncio.to_thread(self.db.backup, target)
        size = path.stat().st_size
        self.db.audit(message.author.id, "backup", str(path))
        await message.channel.send(
            f"💾 Backup written: `{path.name}` ({human_bytes(size)}).\n"
            f"Full path: `{path}`\n"
            "On a Render free instance this file is lost on redeploy — download it if you need it."
        )

    async def _cmd_testai(self, message: discord.Message) -> None:
        status = await message.channel.send("🧪 Testing each provider independently…")
        probe = [
            {"role": "system", "content": "Reply with exactly: PROVIDER_OK"},
            {"role": "user", "content": "Connection test"},
        ]
        lines: list[str] = []
        for name in self.router.order:
            provider = self.router.providers[name]
            if not provider.configured():
                lines.append(f"⚪ {name}: not configured")
                continue
            try:
                # Each attempt inside the provider is capped at 30s, so three
                # retries plus backoff fit inside this wait.
                text = await asyncio.wait_for(
                    asyncio.to_thread(
                        provider.complete, probe, temperature=0.0, max_tokens=32, timeout=30
                    ),
                    timeout=120,
                )
                lines.append(f"✅ {name} (`{provider.current_model()}`): {truncate(text, 80)}")
            except ProviderError as exc:
                lines.append(f"❌ {name}: {truncate(exc.detail, 700)}")
            except asyncio.TimeoutError:
                last = ""
                for row in self.router.status():
                    if row["provider"] == name and row.get("last_error"):
                        last = truncate(row["last_error"].get("message", ""), 300)
                lines.append(
                    f"❌ {name}: still retrying after 120s (provider is slow or rate limited)"
                    + (f" — last error: {last}" if last else "")
                )
            except Exception as exc:  # noqa: BLE001
                lines.append(f"❌ {name}: {type(exc).__name__}: {self._clean(exc)}")
        await status.edit(content=truncate(self._clean("\n".join(lines)), 1_900))

    def _connectors_text(self) -> str:
        if self.connectors is None:
            return "Connector registry is not initialised."
        rows = self.connectors.status()
        active = [row for row in rows if row["enabled"]]
        lines = [f"**Connectors** — {len(active)}/{len(rows)} connected"]
        for row in rows:
            if row["enabled"]:
                state = f"connected ({row['tool_count']} tools)"
            elif not row["configured"]:
                state = f"not configured — needs {', '.join(row['missing_env']) or 'env vars'}"
            else:
                state = "configured but disconnected"
            settings = ", ".join(f"{k}={v}" for k, v in row["settings"].items())
            lines.append(f"• **{row['name']}** — {state}" + (f" [{settings}]" if settings else ""))
        lines.append(
            "Connect with `!connect github`, disconnect with `!disconnect github`. "
            "State is saved in the database; credentials stay in environment variables."
        )
        return "\n".join(lines)

    async def _cmd_connect(self, message: discord.Message, command: str, arg: str) -> None:
        if self.connectors is None:
            await message.channel.send("Connector registry is not initialised.")
            return
        name = (arg or "").strip().lower()
        if not name:
            await message.channel.send(
                f"Usage: `{command} <name>`. Available: {', '.join(self.connectors.names())}"
            )
            return
        try:
            if command == "!connect":
                text = await asyncio.to_thread(self.connectors.enable, name)
            else:
                text = await asyncio.to_thread(self.connectors.disable, name)
        except ConnectorError as exc:
            await message.channel.send(f"❌ {exc}")
            return
        self.db.audit(message.author.id, f"connector:{command[1:]}", name, ok=True)
        await message.channel.send(f"✅ {text}")

    async def _cmd_connector(self, message: discord.Message, arg: str) -> None:
        if self.connectors is None:
            await message.channel.send("Connector registry is not initialised.")
            return
        parts = (arg or "").split(maxsplit=1)
        if not parts or not parts[0]:
            await message.channel.send(
                "Usage: `!connector <name> [test]` or `!connector <name> set key=value`. "
                f"Available: {', '.join(self.connectors.names())}"
            )
            return
        name = parts[0].strip().lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        connector = self.connectors.get(name)
        if connector is None:
            await message.channel.send(
                f"Unknown connector `{name}`. Available: {', '.join(self.connectors.names())}"
            )
            return

        if rest.startswith("set "):
            assignment = rest[4:].strip()
            if "=" not in assignment:
                await message.channel.send("Usage: `!connector <name> set key=value`")
                return
            key, value = assignment.split("=", 1)
            try:
                text = await asyncio.to_thread(
                    self.connectors.set_setting, name, key.strip(), value.strip()
                )
            except ConnectorError as exc:
                await message.channel.send(f"❌ {exc}")
                return
            self.db.audit(message.author.id, "connector:setting", f"{name} {key.strip()}", ok=True)
            await message.channel.send(f"💾 {text}")
            return

        if rest == "test":
            status = await message.channel.send(f"🔌 Testing {connector.title}…")
            try:
                text = await asyncio.wait_for(
                    asyncio.to_thread(connector.check), timeout=45
                )
                await status.edit(content=self._clean(f"✅ {connector.title}: {text}"))
            except ConnectorError as exc:
                await status.edit(content=self._clean(f"❌ {connector.title}: {exc}"))
            except asyncio.TimeoutError:
                await status.edit(content=f"❌ {connector.title}: check timed out after 45s")
            except Exception as exc:  # noqa: BLE001
                await status.edit(
                    content=self._clean(f"❌ {connector.title}: {type(exc).__name__}: {exc}")
                )
            return

        info = connector.status()
        state = (
            "connected" if self.connectors.is_active(name)
            else ("configured but disconnected" if info["configured"] else "not configured")
        )
        lines = [
            f"**{info['title']}** — {state}",
            info["description"],
            "Credentials (environment variables): "
            + ", ".join(f"`{key}`" for key in info["env_vars"]),
        ]
        if info["missing_env"]:
            lines.append("Missing: " + ", ".join(f"`{key}`" for key in info["missing_env"]))
        if info["settings_schema"]:
            saved = ", ".join(f"{k}={v}" for k, v in info["settings"].items()) or "none"
            lines.append(f"Settings: {saved}")
            for key, help_text in info["settings_schema"].items():
                lines.append(f"  `{key}` — {help_text}")
        if info["tools"]:
            lines.append("Tools:")
            for tool in info["tools"]:
                marker = " (write, needs approval)" if tool["write"] else ""
                lines.append(f"  `{tool['name']}`{marker} — {tool['description']}")
        if info["docs_url"]:
            lines.append(info["docs_url"])
        await self._send(message.channel, "\n".join(lines))

    # -- approvals ----------------------------------------------------------
    async def _cmd_approval(self, message: discord.Message, command: str, arg: str) -> None:
        if not arg:
            await message.channel.send(f"Usage: `{command} <id>`. Check `!status` for pending IDs.")
            return
        approval = self.approvals.pop(arg, message.author.id)
        if approval is None:
            await message.channel.send("Unknown, expired, or already-used approval ID.")
            return

        if command == "!deny":
            self.db.audit(message.author.id, "command_denied", approval.command, ok=True)
            await message.channel.send(f"🚫 Denied `{approval.command[:300]}`.")
            run = await self.agent_loop.resume(
                approval,
                command_result="(not run)",
                notify=self._noop_notify,
                approved=False,
            )
            if run.text:
                await self._send(message.channel, run.text)
            return

        if approval.kind == "connector":
            await self._run_approved_connector(message, approval)
            return

        await message.channel.send(f"▶️ Running approved command: `{approval.command[:300]}`")
        result = await asyncio.to_thread(self.runner.run, approval.command)
        self.db.audit(
            message.author.id,
            "command_executed",
            f"{approval.command} -> {result.summary()[:500]}",
            ok=result.ok,
        )
        await self._send(
            message.channel,
            f"**Result** ({result.summary().splitlines()[0]})\n```text\n"
            f"{truncate(result.output, 1_400)}\n```",
        )
        status = StatusMessage(await message.channel.send("🧠 Continuing after the command…"))
        run = await self.agent_loop.resume(
            approval,
            command_result=result.summary(),
            notify=self._notify(status),
            approved=True,
        )
        await self._deliver(message.channel, run, status)

    async def _run_approved_connector(self, message: discord.Message, approval) -> None:
        """Run an owner-approved connector write, then let the agent continue."""
        if self.connectors is None:
            await message.channel.send("Connector registry is not initialised.")
            return
        await message.channel.send(f"▶️ Running approved connector action: `{approval.command[:300]}`")
        try:
            result_text = await asyncio.wait_for(
                asyncio.to_thread(self.connectors.call, approval.tool, approval.args),
                timeout=60,
            )
            ok = True
        except ConnectorError as exc:
            result_text, ok = f"failed: {exc}", False
        except asyncio.TimeoutError:
            result_text, ok = "failed: connector call timed out after 60s", False
        except Exception as exc:  # noqa: BLE001
            result_text, ok = f"failed: {type(exc).__name__}: {exc}", False

        self.db.audit(
            message.author.id,
            "connector_executed",
            f"{approval.tool} -> {truncate(result_text, 400)}",
            ok=ok,
        )
        await self._send(
            message.channel,
            f"**Connector result** ({approval.tool})\n```text\n{truncate(result_text, 1_400)}\n```",
        )
        status = StatusMessage(await message.channel.send("🧠 Continuing after the connector action…"))
        run = await self.agent_loop.resume(
            approval,
            command_result=result_text,
            notify=self._notify(status),
            approved=True,
        )
        await self._deliver(message.channel, run, status)

    # -- task runner --------------------------------------------------------
    async def _handle_task(self, message: discord.Message, text: str) -> None:
        now = time.time()
        last = self._last_task.get(message.author.id, 0.0)
        if self.settings.task_rate_limit and now - last < self.settings.task_rate_limit:
            wait = int(self.settings.task_rate_limit - (now - last))
            await message.channel.send(f"⏳ Slow down — try again in {wait}s.")
            return

        status = StatusMessage(await message.channel.send("🧠 Hermes is working…"))
        async with self._task_lock:
            self._last_task[message.author.id] = time.time()
            self._tasks_run += 1
            run = await self.agent_loop.handle(
                user_id=message.author.id,
                channel_id=message.channel.id,
                prompt=text,
                notify=self._notify(status),
            )
            await self._deliver(message.channel, run, status)

    def _notify(self, status: StatusMessage):
        async def notify(kind: str, text: str) -> None:
            icon = "⚙️" if kind == "status" else "🧠"
            await status.update(f"{icon} {text}")

        return notify

    async def _noop_notify(self, kind: str, text: str) -> None:
        return None

    async def _deliver(self, channel: Any, run: AgentRun, status: StatusMessage) -> None:
        header = {
            "completed": f"✅ **Done** ({run.provider or 'unknown'}, {run.steps} step(s))",
            "awaiting_approval": "⏸️ **Approval required**",
            "failed": "❌ **Failed**",
            "step_limit": "⚠️ **Step limit reached**",
        }.get(run.status, "ℹ️ **Result**")

        if run.status == "awaiting_approval" and run.approval:
            approval = run.approval
            body = (
                f"{header}\nID: `{approval.id}`\n"
                f"Reason: {truncate(approval.reason, 300)}\n"
                f"```text\n{truncate(approval.command, 700)}\n```\n"
                f"Approve with `!approve {approval.id}` or reject with `!deny {approval.id}`. "
                f"Expires in {human_seconds(self.settings.approval_ttl)}."
            )
            await status.finish(self._clean(body))
            return

        text = self._clean(run.text or "(no output)")
        first = f"{header}\n{text}"
        if len(first) <= 1_900:
            await status.finish(first)
            return
        await status.finish(header)
        for chunk in chunk_text(text, 1_900):
            await channel.send(chunk)
