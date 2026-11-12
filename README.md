# Hermes 3.0

An owner-only Discord bot that acts as a controlled autonomous engineer: it plans,
writes and edits files inside its own workspace, runs Python/Node tooling behind an
explicit approval gate, keeps persistent SQLite memory, talks to GitHub, Notion, Slack
and other services through pluggable **connectors**, and falls back between Groq and
Google Gemini when a provider fails.

```
Discord (owner only)
        │  task text
        ▼
  AgentLoop ──► Router ──► Groq  ┐
        │            └──► Gemini ┘ (auto model discovery, retries, cooldown)
        │  one JSON action per step
        ▼
  ToolBox ──► Workspace (files, zip, search)      confined to DATA_DIR/workspace
        └───► CommandRunner (allowlist, shell=False)  requires !approve
        │
        ▼
  SQLite: chat memory · project notes · task history · audit log
Flask:  /health  /ready  /           (Render health checks)
```

## What is new in 3.0

* **Gemini actually works.** Official `google-genai` SDK, model discovery through
  `client.models.list()`, real multi-turn `contents`, `system_instruction`
  preserved, quota/auth/model/blocked errors reported verbatim, retry with
  backoff, automatic re-discovery if a model ID is retired.
* **Web access** — `web_fetch` / `web_links` (strict SSRF guard, stdlib HTML→text),
  `web_search` (Tavily/Brave/SearXNG) and an opt-in headless browser for JavaScript
  pages. See [docs/BROWSER.md](docs/BROWSER.md).
* **Provider fallback** with per-provider cooldown and a `!model` switch.
* **Model-availability checks for both providers** — Groq and Gemini IDs are checked
  against the account's model list and a working replacement is chosen when one is
  retired (Groq shut down `llama-3.3-70b-versatile` on 16 Aug 2026).
* **Structured tool calling** — 14 validated actions; unknown keys are ignored and
  unknown actions refused. No arbitrary code path from model text.
* **Approval gate** for every shell command, with expiry, denial path and audit log.
* **Memory**: conversation history, project notes, task history, audit trail,
  online SQLite backups and forward-only migrations.
* **Connectors** — GitHub, Notion, Slack, Render, Vercel, Cloudflare and Hugging Face
  as pluggable tools: credentials from env vars, enabled state and settings saved in
  SQLite, read tools free, write tools behind `!approve`. See
  [docs/CONNECTORS.md](docs/CONNECTORS.md).
* **Discord commands**: `!help !health !status !model !models !workspace !memory
  !reset !tasks !approve !deny !backup !testai !connectors !connect !disconnect
  !connector !restart`.
* **Honest limits**: no claim of a test/commit/deploy that a tool result did not
  produce, and `python agent.py --check` for a real preflight.

Full command list and environment variables: below. Bug-by-bug audit of the 2.0
code and what changed: [docs/UPGRADE_NOTES.md](docs/UPGRADE_NOTES.md).

## Quick start (local)

```bash
git clone https://github.com/Indvishal69/hermes-bot.git
cd hermes-bot
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then fill it in (never commit .env)
```

Set at minimum in `.env` (or export them):

```bash
DISCORD_TOKEN=...        # Bot token from discord.com/developers/applications
ALLOWED_USER_ID=...      # your numeric Discord user ID
GROQ_API_KEY=...         # and/or GEMINI_API_KEY
```

Validate without starting Discord:

```bash
python agent.py --check
```

Expected output on a healthy setup:

```
Hermes 3.0 3.0.0 — preflight
  storage: /path/data writable
  storage: /path/data/workspace writable
  storage: /path/data/backups writable
  database: /path/data/hermes.sqlite3 ready (schema v2)
  provider groq: configured
  provider gemini: configured
RESULT: OK
```

Run it:

```bash
python agent.py
```

Then in Discord, DM the bot or mention it in a channel it can read: `!health`, then
`hello`, then a real task such as *"create a small Flask app in `demo/` with a
/health route and a test, then show me the files"*.

**Discord setup requirements**

1. Create an application → Bot → copy the token.
2. Enable **Message Content Intent** (Bot → Privileged Gateway Intents). Without it
   the bot receives empty message text.
3. Invite with scopes `bot` and permissions *Send Messages*, *Read Message History*,
   *Embed Links*; the bot only needs to read its owner's messages.

## Discord commands

| Command | Purpose |
| --- | --- |
| `!help` | Command list |
| `!health` | DB, workspace, provider state, last provider errors |
| `!status` | Uptime, task counts, pending approvals, recent commands |
| `!model` | Show active provider and models |
| `!model groq\|gemini` | Switch preferred provider for this process |
| `!models` | List text models your API keys can actually use |
| `!workspace` | Directory tree of the workspace |
| `!memory` | Memory stats and recent project notes |
| `!memory <text>` | Save a project note |
| `!memory clear` | Delete project notes |
| `!reset` | Clear your conversation history |
| `!tasks` | Recent task history |
| `!approve <id>` | Run a command the agent requested |
| `!deny <id>` | Reject it and let the agent adapt |
| `!backup` | Write a consistent SQLite backup |
| `!testai` | Test each provider independently, showing real errors |
| `!connectors` | Connectors: connected / configured / missing env vars |
| `!connect NAME` / `!disconnect NAME` | Enable or disable a connector (saved) |
| `!connector NAME [test]` | Detail, live check |
| `!connector NAME set key=value` | Save a non-secret setting |
| `!web <url>` | Fetch a page and show what the agent would read |
| `!restart` | Restart the process (`os.execv`) |

Anything else is treated as a task. Tasks are rate-limited (`TASK_RATE_LIMIT`,
default 3s) and serialised, so two tasks cannot interleave their tool calls.

## Environment variables

Required: `DISCORD_TOKEN`, `ALLOWED_USER_ID`, and at least one of `GROQ_API_KEY` /
`GEMINI_API_KEY`.

| Variable | Default | Notes |
| --- | --- | --- |
| `ACTIVE_PROVIDER` | `groq` | `groq` or `gemini`; the other is the fallback |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Any Groq chat model; if the ID is retired or not on your plan, a working chat model is selected automatically |
| `GEMINI_MODEL` | *(empty)* | Empty = auto-discover a text model for the key |
| `GEMINI_API_BASE` | *(empty)* | Only for a compatible proxy/endpoint |
| `MAX_AGENT_STEPS` | `8` | Tool steps per task (1–25) |
| `AI_TIMEOUT` | `150` | Seconds per model call |
| `MAX_HISTORY_MESSAGES` | `20` | Conversation turns sent back to the model |
| `TEMPERATURE` | `0.2` | 0–2 |
| `MAX_COMPLETION_TOKENS` | `3000` | Per response |
| `PROVIDER_COOLDOWN` | `60` | Seconds a failing provider is deprioritised |
| `TASK_RATE_LIMIT` | `3` | Minimum seconds between tasks |
| `DATA_DIR` | `./data` | SQLite, backups, workspace |
| `WORKSPACE_DIR` | `$DATA_DIR/workspace` | The only directory the agent may touch |
| `MAX_FILE_BYTES` | `200000` | Per-file read/write limit |
| `MAX_OUTPUT_CHARS` | `7000` | Command/file output sent to the model |
| `COMMAND_TIMEOUT` | `45` | Hard limit per command |
| `APPROVAL_TTL` | `900` | Seconds before a pending approval expires |
| `ALLOW_GIT_PUSH` | `false` | Keep false unless you want `git push` possible |
| `PORT` | `10000` | Flask health server |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING` |
| `GITHUB_TOKEN` | *(empty)* | GitHub connector (fine-grained: Contents + Issues read/write) |
| `NOTION_TOKEN` | *(empty)* | Notion connector |
| `SLACK_BOT_TOKEN` | *(empty)* | Slack connector (`channels:read`, `chat:write`) |
| `RENDER_API_KEY` | *(empty)* | Render connector |
| `VERCEL_TOKEN` | *(empty)* | Vercel connector |
| `CLOUDFLARE_API_TOKEN` | *(empty)* | Cloudflare connector |
| `HF_TOKEN` | *(empty)* | Hugging Face connector |
| `WEB_ENABLED` | `true` | Web fetch/link/search tools |
| `WEB_TIMEOUT` / `WEB_MAX_BYTES` / `WEB_MAX_CHARS` | `20` / `2000000` / `7000` | Fetch limits |
| `WEB_ALLOWED_DOMAINS` | *(empty)* | Optional comma-separated allowlist |
| `SEARCH_PROVIDER` / `SEARCH_API_KEY` / `SEARXNG_URL` | *(empty)* | Web search backend |
| `BROWSER_ENABLED` | `false` | Headless Chromium for JS pages (needs ≥1 GB RAM) |
| `BROWSER_TIMEOUT` | `30` | Page load timeout |

Bad values never crash the process: they are reported, and the documented default
is used (for example `MAX_AGENT_STEPS=many` logs a warning and uses 8).

## Repository layout

```
agent.py                     entry point (--check, --version)
hermes/
  config.py                  env parsing, validation, secret redaction list
  db.py                      SQLite + migrations + backup/restore
  utils.py                   redaction, JSON extraction, backoff, chunking
  providers/
    base.py                  Provider interface, ProviderError taxonomy
    groq.py                  Groq chat completions with retries
    gemini.py                google-genai SDK, model discovery, error mapping
    router.py                preference order, fallback, cooldown
  tools/
    files.py                 workspace-confined file operations
    shell.py                 command guard + subprocess runner
    registry.py              tool schema and dispatcher
  core/loop.py               plan → act → observe, approvals
  bot.py                     Discord commands and status messages (agent loop is `agent_loop`)
  web.py                     Flask health endpoints
  connectors/                pluggable services: base, registry, github, notion,
                             slack, platforms (render/vercel/cloudflare/hf)
  browsing/                  web fetch + SSRF guard, search, optional browser
tests/                       275 pytest tests (no network, no credentials needed)
docs/                        deployment, Gemini troubleshooting, security, upgrade notes
```

## Tests

```bash
pip install pytest
python -m pytest -q
```

The suite needs no API keys and no network: provider behaviour is exercised with
fakes, plus a real local HTTP mock for the Gemini and Groq request paths. Live
provider calls can only be verified with your own keys — use `!testai` for that.

## Deployment

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for Render (including what the free
plan really does to a Discord bot), Railway, Fly.io and a plain VPS, and
[docs/CONNECTORS.md](docs/CONNECTORS.md) for the connector system and
[docs/BROWSER.md](docs/BROWSER.md) for web access.

## License / ownership

Private project. No license file is included; all rights stay with the repository
owner.
