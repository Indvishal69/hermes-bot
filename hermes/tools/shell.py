"""Controlled command execution.

Rules enforced here (not merely requested from the model):

* ``shell=False`` always — no shell, so no globbing, pipes or redirection.
* Executable allowlist and a subcommand allowlist for ``git``.
* Blocked binaries, destructive flags and secret-dumping patterns.
* A minimal environment allowlist: the bot's own env (tokens, keys) is never
  inherited by a child process.
* Hard timeout, output cap and a registry of running processes.

Every command still needs an explicit ``!approve`` from the owner before it
runs; this module is the second line of defence, not the first.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..utils import human_seconds, truncate

log = logging.getLogger("hermes.tools.shell")

ALLOWED_EXECUTABLES = {
    "python",
    "python3",
    "pip",
    "pip3",
    "node",
    "npm",
    "npx",
    "pytest",
    "git",
}

BLOCKED_EXECUTABLES = {
    "sudo", "su", "doas", "rm", "rmdir", "mkfs", "mke2fs", "dd", "shutdown",
    "reboot", "poweroff", "halt", "mount", "umount", "iptables", "ufw",
    "passwd", "useradd", "userdel", "usermod", "chmod", "chown", "curl",
    "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "kill", "pkill",
    "killall", "systemctl", "service", "apt", "apt-get", "dnf", "yum", "brew",
    "sh", "bash", "zsh", "dash", "perl", "ruby", "php",
}

SHELL_METACHARACTERS = ("&&", "||", ";", "|", ">", "<", "`", "$(", "\n", "\r", "\x00")

GIT_ALLOWED_SUBCOMMANDS = {
    "status", "log", "diff", "show", "add", "commit", "branch", "checkout",
    "switch", "init", "remote", "fetch", "pull", "stash", "rev-parse",
    "ls-files", "config", "tag", "describe", "restore",
}
GIT_DANGEROUS_ARGS = {"--force", "-f", "--hard", "--mirror", "--no-verify"}
GIT_BLOCKED_SUBCOMMANDS = {"reset", "clean", "push", "filter-branch", "reflog"}

# Patterns that would dump credentials or the host environment.
SECRET_PATTERNS = (
    re.compile(r"os\.environ", re.I),
    re.compile(r"process\.env", re.I),
    re.compile(r"printenv", re.I),
    re.compile(r"\benv\b"),
    re.compile(r"\.env\b"),
    re.compile(r"\b(api[_-]?key|secret|password|passwd|token|credential)s?\b", re.I),
    re.compile(r"/proc/self/environ", re.I),
)

ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "PYTHONHASHSEED")
MAX_COMMAND_LENGTH = 500


class CommandRejected(ValueError):
    """The command is not allowed to run at all."""


@dataclass
class CommandResult:
    command: str
    exit_code: int | None
    output: str
    duration: float
    timed_out: bool = False
    rejected: bool = False

    @property
    def ok(self) -> bool:
        return not self.rejected and not self.timed_out and self.exit_code == 0

    def summary(self) -> str:
        if self.rejected:
            return f"Rejected: {self.output}"
        if self.timed_out:
            return f"Timed out after {human_seconds(self.duration)}.\n{self.output}"
        return f"exit={self.exit_code} in {human_seconds(self.duration)}\n{self.output}"


@dataclass
class _Process:
    pid: int
    command: str
    started: float
    finished: float | None = None
    exit_code: int | None = None


class CommandRunner:
    def __init__(
        self,
        workspace: Path,
        *,
        timeout: int = 45,
        max_output_chars: int = 7_000,
        allow_git_push: bool = False,
        keep_history: int = 50,
    ):
        self.workspace = Path(workspace)
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self.allow_git_push = allow_git_push
        self.keep_history = keep_history
        self._processes: list[_Process] = []
        self._lock = threading.Lock()

    # -- validation ---------------------------------------------------------
    def validate(self, command: str) -> list[str]:
        """Return argv for a safe command or raise :class:`CommandRejected`."""
        command = (command or "").strip()
        if not command:
            raise CommandRejected("Empty command.")
        if len(command) > MAX_COMMAND_LENGTH:
            raise CommandRejected(f"Command longer than {MAX_COMMAND_LENGTH} characters.")
        for token in SHELL_METACHARACTERS:
            if token in command:
                raise CommandRejected(
                    "Shell operators, redirection and subshells are disabled. "
                    "Send one plain command at a time."
                )
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise CommandRejected(f"Could not parse command: {exc}") from exc
        if not argv:
            raise CommandRejected("Empty command.")

        exe = Path(argv[0]).name.lower()
        if exe in BLOCKED_EXECUTABLES:
            raise CommandRejected(f"'{exe}' is blocked.")
        if exe not in ALLOWED_EXECUTABLES:
            raise CommandRejected(
                f"'{exe}' is not allowed. Allowed: {', '.join(sorted(ALLOWED_EXECUTABLES))}."
            )
        if "/" in argv[0] or "\\" in argv[0]:
            raise CommandRejected("Use a bare executable name, not a path.")

        lowered = [arg.lower() for arg in argv[1:]]
        for arg in lowered:
            base = Path(arg).name
            if base in BLOCKED_EXECUTABLES:
                raise CommandRejected(f"Argument '{arg}' is a blocked binary.")
        for arg in argv[1:]:
            if arg.startswith("/"):
                raise CommandRejected(
                    "Absolute paths are not allowed in commands; use a workspace-relative path."
                )

        joined = " ".join(argv)
        for pattern in SECRET_PATTERNS:
            if pattern.search(joined):
                raise CommandRejected(
                    "Command appears to read or print secrets/environment variables."
                )

        if exe == "git":
            if len(argv) < 2:
                raise CommandRejected("git needs a subcommand.")
            sub = argv[1].lower()
            if sub in GIT_BLOCKED_SUBCOMMANDS:
                if sub == "push" and self.allow_git_push:
                    pass
                else:
                    raise CommandRejected(f"'git {sub}' is blocked on this agent.")
            elif sub not in GIT_ALLOWED_SUBCOMMANDS:
                raise CommandRejected(f"'git {sub}' is not allowed.")
            if sub == "push":
                for arg in lowered:
                    if arg in {"--force", "-f", "--force-with-lease"}:
                        raise CommandRejected("Force-push is blocked.")
            for arg in lowered:
                if arg in GIT_DANGEROUS_ARGS:
                    raise CommandRejected(f"'{arg}' is blocked for git.")
        return argv

    def is_allowed(self, command: str) -> tuple[bool, str]:
        try:
            self.validate(command)
            return True, ""
        except CommandRejected as exc:
            return False, str(exc)

    # -- execution ----------------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
        tmp_dir = self.workspace / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "HOME": str(self.workspace),
                "TMPDIR": str(tmp_dir),
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "CI": "1",
                "NO_COLOR": "1",
                "npm_config_cache": str(self.workspace / ".npm-cache"),
                "npm_config_update_notifier": "false",
            }
        )
        return env

    def run(self, command: str, *, timeout: int | None = None) -> CommandResult:
        started = time.time()
        try:
            argv = self.validate(command)
        except CommandRejected as exc:
            return CommandResult(
                command=command, exit_code=None, output=str(exc), duration=0.0, rejected=True
            )

        timeout = int(timeout or self.timeout)
        log.info("Running approved command: %s", command)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(self.workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                shell=False,
                env=self._env(),
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            return CommandResult(
                command=command,
                exit_code=None,
                output=f"Could not start: {type(exc).__name__}: {exc}",
                duration=time.time() - started,
            )

        record = _Process(pid=proc.pid, command=command, started=started)
        with self._lock:
            self._processes.append(record)
            self._processes = self._processes[-self.keep_history :]

        timed_out = False
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(proc)
            try:
                output, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                output = ""
        duration = time.time() - started
        record.finished = time.time()
        record.exit_code = proc.returncode
        body = (output or "").strip() or "(no output)"
        return CommandResult(
            command=command,
            exit_code=proc.returncode,
            output=truncate(body, self.max_output_chars),
            duration=duration,
            timed_out=timed_out,
        )

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                return
        time.sleep(1)
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

    # -- monitoring ---------------------------------------------------------
    def active(self) -> list[dict[str, object]]:
        with self._lock:
            records = list(self._processes)
        return [
            {
                "pid": rec.pid,
                "command": rec.command[:120],
                "running": rec.finished is None,
                "seconds": round((rec.finished or time.time()) - rec.started, 1),
                "exit_code": rec.exit_code,
            }
            for rec in records
        ]

    def python_syntax_check(self, relative_paths: Iterable[str]) -> str:
        """Compile Python files in the workspace without executing them."""
        results: list[str] = []
        for rel in relative_paths:
            path = (self.workspace / rel).resolve()
            if self.workspace.resolve() not in path.parents or not path.is_file():
                results.append(f"{rel}: skipped (outside workspace or missing)")
                continue
            try:
                source = path.read_text(encoding="utf-8")
                compile(source, str(path), "exec")
                results.append(f"{rel}: OK")
            except SyntaxError as exc:
                results.append(f"{rel}: SyntaxError line {exc.lineno}: {exc.msg}")
            except (UnicodeDecodeError, OSError) as exc:
                results.append(f"{rel}: unreadable ({type(exc).__name__})")
        return "\n".join(results) or "(no files checked)"
