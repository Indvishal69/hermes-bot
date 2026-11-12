# Security model

Hermes is deliberately powerful and deliberately fenced. Nothing below can be
switched off by the model, by a Discord message, or by a clever prompt — the fences
are in code, and each one has a test.

## Threat model

| Threat | Control |
| --- | --- |
| A stranger finds the bot and gives it orders | Owner-only gate on `ALLOWED_USER_ID`; every other message is ignored and audit-logged |
| The model asks to run `rm -rf /` | Two independent gates: command allowlist/denylist, then owner `!approve` |
| The model reads `/etc/passwd` or `../../.env` | Workspace confinement on every path; absolute paths and `..` rejected after `Path.resolve()` (so symlinks cannot escape either) |
| The model exfiltrates the bot's own tokens | Child processes get a minimal env allowlist (`PATH`, `LANG`, `TZ`, `HOME`, `TMPDIR`, …); `DISCORD_TOKEN`/API keys are never inherited |
| A secret leaks into a log or Discord message | `redact()` removes known secret values plus Google/Groq/GitHub/HF key shapes from every error, tool result and status edit |
| Prompt injection from a file the agent reads | File content is data. Tool calls are validated by the registry; nothing in a file can call a tool, and commands still need human approval |
| Runaway command | Hard `COMMAND_TIMEOUT`, output cap, process-group kill (SIGTERM → SIGKILL) |
| Runaway agent | `MAX_AGENT_STEPS`, `AI_TIMEOUT`, task rate limit, single-task lock |
| Destructive file operation | Deletion is one file or an empty directory; `.git` writes refused; workspace root can never be deleted or written |
| Unbounded memory growth | Per-user message cap, note cap, audit retention by row count, expiring approvals |
| A bad deploy destroys data | Additive migrations, `!backup` (SQLite online backup API), `Database.restore()` validates the file first |

## The approval gate

A shell command only runs after the owner types `!approve <id>`:

1. The model returns `{"action":"run_command","command":"...","reason":"..."}`.
2. `CommandRunner.validate()` runs **before** the approval prompt, so a policy
   violation is rejected without bothering the owner.
3. The pending approval stores the exact conversation state, expires after
   `APPROVAL_TTL` (default 900s), and is bound to one user id.
4. `!deny` resumes the task with an explicit "not run" result so the model adapts
   instead of hallucinating success.
5. Both request and outcome are written to `audit_log`.

## What is allowed to run

* Executables: `python`, `python3`, `pip`, `pip3`, `node`, `npm`, `npx`, `pytest`,
  `git` — bare names only, never paths.
* `git`: read/write subcommands only (`status log diff show add commit branch
  checkout switch init remote fetch pull stash rev-parse ls-files config tag
  describe restore`). `push` is refused unless `ALLOW_GIT_PUSH=true`; `reset`,
  `clean`, `filter-branch`, `reflog`, `--force` and `--hard` are always refused.
* No shell: `shell=False`, and `&&`, `||`, `;`, `|`, `>`, `<`, backticks, `$(`
  and newlines are rejected outright, so pipes and redirection are impossible.
* Blocked binaries include `sudo`, `su`, `rm`, `dd`, `mkfs`, `mount`, `chmod`,
  `chown`, `curl`, `wget`, `nc`, `ssh`, `kill`, `systemctl`, `apt`, and the shells
  themselves.
* Secret-dumping patterns are rejected: `os.environ`, `process.env`, `printenv`,
  standalone `env`, `.env`, `/proc/self/environ`, and words like `api_key` /
  `token` / `secret` in argument position.

False positives are treated as acceptable: if a legitimate command is blocked, the
owner runs it manually. `pip install python-dotenv` is explicitly allowed — the 2.0
guard rejected it because it matched the substring `env`, which is a bug, not a
feature (see `tests/test_shell.py`).

## Data handling

* SQLite lives at `DATA_DIR/hermes.sqlite3` with WAL mode; only the bot process
  writes to it.
* Chat memory keeps the most recent 120 messages per user (`max_messages_per_user`).
* Audit rows record actor, action, detail (truncated) and success flag. They never
  contain file bodies or secrets.
* Backups go to `DATA_DIR/backups/`. On an ephemeral host they vanish on redeploy —
  copy them off the host if they matter.
* Approvals are in memory only: a restart cancels every pending command. That is
  intentional.

## Integrations

`hermes/integrations/` holds GitHub, Render, Vercel, Cloudflare and Hugging Face
clients. They are **prepared, not wired**: nothing calls them automatically, and the
model has no tool that reaches them. Each uses a token from an environment variable,
redacts it from errors, and fails loudly when unconfigured. Add a tool only when you
are ready to give the agent that power, and route writes through the approval gate.

## If you are deploying publicly

* Keep the Discord app private (no public bot listing).
* Keep `ALLOWED_USER_ID` to your own id only.
* Never set `ALLOW_GIT_PUSH=true` on a host you do not control.
* Rotate any key that has ever appeared in a log, screenshot or commit.
* The health endpoint deliberately exposes no paths, models or secrets; keep it that
  way if you add fields.

## Reporting

This is a single-user private project. If you find a bypass, fix it in code and add a
regression test in `tests/` — a rule that is not tested will eventually be lost.
