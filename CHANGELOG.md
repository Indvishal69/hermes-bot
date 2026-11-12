# Changelog

## 3.2.0 — 12 November 2026

**New: web access for the agent.**

* `hermes/browsing/`: `web_fetch` and `web_links` (plain HTTP + stdlib HTML→text),
  `web_search` (Tavily / Brave / SearXNG), and `browser_open` (headless Chromium via
  Playwright, opt-in).
* SSRF guard on every URL: http/https only, no embedded credentials, ports 80/443 only,
  `localhost`/`*.internal` blocked, IP literals checked directly, hostnames refused when
  they resolve to loopback/RFC1918/link-local/CGNAT/reserved, and every redirect hop
  re-validated (max 3). Optional `WEB_ALLOWED_DOMAINS` allowlist.
* Fetch limits: 2 MB body cap, 7000 chars to the model, 10s connect / `WEB_TIMEOUT` read,
  non-text content refused with a clear message.
* Prompt-injection stance made explicit: fetched pages and search results are returned
  with an "untrusted data" header and a matching system-prompt rule; the approval gate
  still covers every write.
* `!web <url>` shows exactly what the agent would read; `!health` reports fetch/search/
  browser capability.
* The headless browser is off by default and documented as needing ≥1 GB RAM — Chromium
  does not fit a Render free instance. When Playwright is missing, `browser_open` says so
  with the exact install commands.
* Tests: 37 browsing tests plus loop coverage — 275 total.
* Docs: `docs/BROWSER.md`.

## 3.1.0 — 12 November 2026

**New: a connector system.** External services are now pluggable tools for the agent,
with saved state.

* `hermes/connectors/`: `Connector` base class, `ConnectorRegistry`, and seven
  connectors — GitHub, Notion, Slack, Render, Vercel, Cloudflare, Hugging Face (20
  tools). Replaces the earlier `hermes/integrations/` package, which was prepared but
  never reachable by the model.
* Credentials come from environment variables only; enabled/disabled state and
  non-secret settings (default repo, database id, channel, service, zone) are stored in
  a new `connectors` table (schema v3) and survive restarts.
* Read tools run freely; write tools (`github_create_issue`, `github_commit_file`,
  `notion_append_text`, `slack_post_message`, `render_trigger_deploy`,
  `cloudflare_purge_cache`) go through the same `!approve` gate as shell commands. The
  approval payload now carries a `kind`, so `!approve` knows whether to run a command
  or a connector action.
* New commands: `!connectors` (alias `!integrations`), `!connect`, `!disconnect`,
  `!connector <name>`, `!connector <name> test`, `!connector <name> set key=value`.
* Model input is validated as identifiers before any request; a tool call cannot choose
  a host or traverse paths. Every connector error is redacted.
* `docs/CONNECTORS.md`: how it works, token scopes, settings, and how to add your own
  connector.
* Tests: 41 connector tests plus loop/bot coverage — 235 total.

## 3.0.2 — 12 November 2026

**Fixed: Groq tasks failed with `HTTP 404 model_not_found`, and Gemini 503 bursts
timed out.**

Groq retired `llama-3.3-70b-versatile` (and `llama-3.1-8b-instant`) for free and
developer tiers on 16 August 2026; the recommended replacement is
`openai/gpt-oss-120b`. Hermes shipped the retired ID as its default and, unlike
Gemini, never checked whether a model actually existed for the key.

* `hermes/providers/groq.py`: model discovery via `GET /openai/v1/models`, a
  preference list, and one automatic re-discovery + retry when a call returns
  `404 model_not_found`. Payload now uses `max_completion_tokens`, which every
  current Groq chat model accepts.
* Default `GROQ_MODEL` is `openai/gpt-oss-120b`; a stale value in the environment is
  corrected at runtime instead of failing every message.
* Both providers cap each HTTP attempt (50s normally, 30s in `!testai`) so retries
  cannot outlive `AI_TIMEOUT`; a timed-out call now reports the last provider error.
* `providers/base.py::choose_model` is shared by Groq and Gemini instead of two
  near-identical selectors.
* Provider model IDs are resolved at startup (`on_ready`), so `!health` and `!model`
  show the model actually in use instead of a stale configured value.
* `tests/test_groq.py`: 26 new tests (181 total).
* Docs: README, `.env.example`, `render.yaml`, deployment notes and test report
  updated for the Groq model change.

## 3.0.1 — 12 November 2026

**Fixed: every Discord message failed with `AttributeError: '_UnixSelectorEventLoop'
object has no attribute 'handle'`.**

`discord.Client` has its own `loop` attribute, and `Client._async_setup_hook()`
replaces it with the running asyncio loop when `bot.run()` starts. Hermes 3.0.0 had
stored the `AgentLoop` in `self.loop`, so `self.loop.handle(...)` called
`handle` on the event loop object.

* `hermes/bot.py`: the agent loop is now `self.agent_loop` (constructor plus all three
  call sites), with a comment explaining the collision.
* `tests/test_bot.py`: 11 new tests, including one that reproduces the production
  condition and one that fails if any `discord.Client` attribute is shadowed again.
* Test suite: 143 → 154 tests, all passing.

## 3.0.0 — 12 November 2026

First release of the rewritten agent.

* Modular `hermes/` package: config, db, providers, tools, core loop, bot, web,
  integrations.
* Gemini fixed: official `google-genai` SDK, model discovery via
  `client.models.list()`, real multi-turn `contents`, `system_instruction`
  preserved, classified errors, retry with backoff, re-discovery on retired model IDs.
* Provider fallback with cooldown; `!model` switch; `!models` discovery.
* 14 validated tools; every shell command behind owner approval (`!approve`/`!deny`),
  `shell=False`, executable allowlist, no inherited secrets.
* SQLite migrations, chat/project memory, task history, audit log, online backup.
* 18 owner-only Discord commands including `!models`, `!memory`, `!status`, `!restart`.
* `/health` exposes no paths or secrets; `python agent.py --check` preflight.
* Docs: deployment (Render free-tier limits), Gemini troubleshooting, security model,
  upgrade notes, test report.
* 143 tests.
