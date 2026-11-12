# Changelog

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
