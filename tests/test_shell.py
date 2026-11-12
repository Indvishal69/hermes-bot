"""Command guard: allowlist, blocklist, secret patterns, timeouts, env isolation."""

from __future__ import annotations

import sys

import pytest

from hermes.tools.shell import CommandRejected, CommandRunner


@pytest.fixture()
def runner(workspace):
    return CommandRunner(workspace.root, timeout=10, max_output_chars=2_000)


@pytest.mark.parametrize(
    "command",
    [
        "python app.py",
        "python3 -m pytest -q",
        "pip install python-dotenv",     # regression: 2.0 blocked this ('env' substring)
        "pip3 install requests",
        "pytest tests/test_env.py",      # regression: 'env' inside a filename
        "npm install",
        "npm run build",
        "node server.js",
        "git status",
        "git commit -m fix",
        "git add app.py",
    ],
)
def test_legitimate_commands_are_allowed(runner, command):
    allowed, reason = runner.is_allowed(command)
    assert allowed, f"{command} was rejected: {reason}"


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "sudo python app.py",
        "curl https://example.com",
        "wget http://example.com/x",
        "bash -c 'echo hi'",
        "sh script.sh",
        "chmod 777 app.py",
        "dd if=/dev/zero of=/dev/sda",
        "systemctl restart nginx",
        "kill -9 1",
        "python app.py && rm -rf data",
        "python app.py | tee log",
        "python app.py > out.txt",
        "python -c 'import os;print(os.environ)'",
        "python -c \"print(open('.env').read())\"",
        "git push origin main",
        "git reset --hard HEAD",
        "git clean -fd",
        "python /usr/bin/app.py",
        "/bin/rm -rf /",
    ],
)
def test_dangerous_commands_are_rejected(runner, command):
    allowed, reason = runner.is_allowed(command)
    assert not allowed
    assert reason


def test_git_push_allowed_only_when_enabled(workspace):
    strict = CommandRunner(workspace.root)
    assert not strict.is_allowed("git push origin main")[0]
    permissive = CommandRunner(workspace.root, allow_git_push=True)
    assert permissive.is_allowed("git push origin main")[0]
    assert not permissive.is_allowed("git push --force origin main")[0]


def test_long_command_rejected(runner):
    assert not runner.is_allowed("python " + "a" * 600)[0]


def test_empty_command_rejected(runner):
    assert not runner.is_allowed("   ")[0]


def test_validate_returns_argv(runner):
    assert runner.validate("python -m pytest -q") == ["python", "-m", "pytest", "-q"]


def test_run_executes_and_captures_output(workspace, runner):
    workspace.write_text("hello.py", "print('hello from hermes')\n")
    result = runner.run(f"{sys.executable.split('/')[-1]} hello.py")
    if result.rejected:  # executable name may be versioned (python3.12)
        result = runner.run("python3 hello.py")
    assert result.exit_code == 0
    assert "hello from hermes" in result.output


def test_run_returns_nonzero_exit(workspace, runner):
    workspace.write_text("boom.py", "raise SystemExit(3)\n")
    result = runner.run("python3 boom.py")
    assert result.exit_code == 3
    assert not result.ok


def test_timeout_kills_the_process(workspace):
    workspace.write_text("slow.py", "import time\ntime.sleep(30)\n")
    runner = CommandRunner(workspace.root, timeout=1)
    result = runner.run("python3 slow.py", timeout=1)
    assert result.timed_out
    assert result.duration < 20


def test_rejected_command_never_executes(runner):
    result = runner.run("rm -rf /")
    assert result.rejected
    assert result.exit_code is None


def test_child_process_does_not_inherit_bot_secrets(workspace, runner, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "super-secret-discord-token")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_should_not_leak")
    workspace.write_text("dump.py", "import os\nprint('|'.join(sorted(os.environ)))\n")
    result = runner.run("python3 dump.py")
    assert "DISCORD_TOKEN" not in result.output
    assert "GROQ_API_KEY" not in result.output
    assert "HOME" in result.output


def test_python_syntax_check(workspace, runner):
    workspace.write_text("good.py", "x = 1\n")
    workspace.write_text("bad.py", "def broken(:\n")
    report = runner.python_syntax_check(["good.py", "bad.py", "../outside.py"])
    assert "good.py: OK" in report
    assert "bad.py: SyntaxError" in report
    assert "outside workspace" in report


def test_process_registry_tracks_runs(workspace, runner):
    workspace.write_text("quick.py", "print(1)\n")
    runner.run("python3 quick.py")
    rows = runner.active()
    assert rows and rows[-1]["running"] is False
