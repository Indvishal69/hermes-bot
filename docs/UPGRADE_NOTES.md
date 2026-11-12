# Upgrade notes — Hermes 2.0 → 3.0

What was actually wrong in the uploaded `Hermes_2_0_Update_Package.zip`, what was
verified, and what changed. Line numbers refer to the uploaded `agent.py` (567 lines).

## Repository vs. upload

The GitHub repository `Indvishal69/hermes-bot` `main` branch and the ZIP were **not
the same code**:

| File | GitHub `main` | ZIP |
| --- | --- | --- |
| `agent.py` | ~9 KB, `openai`/`huggingface_hub` era | ~23 KB, Groq + Gemini |
| `requirements.txt` | `discord.py, openai, flask, requests, huggingface_hub` | `discord.py, Flask, requests, google-genai<2` |
| `render.yaml` | absent | present, but broken for the free plan |
| `README.md` | 12 bytes (`# hermes-bot`) | 69 lines |

So "compare the ZIP with the repository" resolves to: the repository is older, and
neither version had the 3.0 capabilities. `openai` and `huggingface_hub` were
installed but unused by the ZIP's code.

## Confirmed bugs in 2.0

| # | Bug | Evidence | Fix in 3.0 |
| --- | --- | --- | --- |
| 1 | `!testai` joined its result lines with a literal `\n` | `agent.py:524` contains `"\\n".join(results)` (a backslash and an `n`), so the reply printed `✅ Groq…\n✅ Gemini…` on one line | `"\n".join(...)` in `bot.py::_cmd_testai`, which also calls each provider directly and shows real error text |
| 2 | Gemini SDK pinned below 2.x | `requirements.txt`: `google-genai>=1.30,<2`; current SDK is 3.x | `google-genai>=1.0,<4`, installed and exercised in tests |
| 3 | No Gemini model discovery despite the spec | `call_gemini` called `generate_content(model=GEMINI_MODEL)` with no `models.list()` | `GeminiProvider.resolve_model()` lists models, filters non-text families, honours the configured ID, else picks a preferred text model, and re-discovers once on `404` |
| 4 | Conversation flattened into one string | `agent.py:191-201` built `USER:\n… / ASSISTANT:\n…` and sent it as `contents=prompt` | Real `contents` list with `user`/`model` roles, consecutive turns merged |
| 5 | Blocked responses lost their reason | `agent.py:213-219` read `response.text`, then raised a generic "no text" error | `_extract_text()` reports `finish_reason`, `prompt_feedback.block_reason` and flagged safety categories |
| 6 | No retry on 429 | `call_groq`/`call_gemini` failed on the first non-200 | Exponential backoff (1.5s, 2.25s…) on quota/network, no retry on auth/model errors |
| 7 | Command guard blocked legitimate work | `agent.py:338`: `any(term in joined for term in ("os.environ","printenv","env","secret","token"))` is a **substring** test, so `pip install python-dotenv` and any path containing `env` were rejected | Word-boundary patterns plus explicit paths (`os.environ`, `process.env`, `.env`, `printenv`); `pip install python-dotenv` and `pytest tests/test_env.py` now pass (`tests/test_shell.py`) |
| 8 | `render.yaml` set `DATA_DIR=/var/data` on `plan: free` with no disk | `render.yaml` in the ZIP | Blueprint rewritten: `DATA_DIR` inside the project, paid plan + disk documented, free-plan data loss spelled out in `docs/DEPLOYMENT.md` |
| 9 | `/` endpoint exposed the absolute workspace path | `agent.py:72` returned `"workspace": str(WORKSPACE)` | `/` and `/health` return booleans and counts only (`tests/test_web_and_utils.py`) |
| 10 | Missing required commands | spec lists `!models !memory !status !restart`; 2.0 had none | All four implemented, plus `!tasks`, `!backup`, `!testai`, `!integrations` |
| 11 | Approvals never expired and could pile up | `pending` dict had no TTL or cap | `ApprovalStore` with TTL (`APPROVAL_TTL`) and a 20-item cap; restart clears them |
| 12 | No audit trail, no task history | no such tables | `audit_log` + `tasks` tables, written for commands, tool calls and task outcomes |
| 13 | Bad env values crashed at import | `MAX_STEPS = int(os.getenv(...))` raises `ValueError` during import | `load_settings()` collects problems, reports them, uses documented defaults |
| 14 | No graceful shutdown, no preflight | `bot.run()` at module level | `agent.py` with `--check`, SIGTERM handler, structured logging |
| 15 | One shared `messages` list per task, no task lock | concurrent messages could interleave tool calls | Single-task `asyncio.Lock` + rate limit; conversation history in SQLite per user |
| 16 | Duplicate provider entry in status output | `build_router` order could repeat a provider (found while testing 3.0) | `Router` de-duplicates its order (`tests/test_router.py`) |

## What is new (beyond fixes)

* `hermes/` package: config, db, providers, tools, core loop, bot, web, integrations.
* 14 validated tools, including `replace_in_file`, `search_files`, `make_zip`,
  `check_python`, `memory_note`, `list_processes`.
* Project memory (`memory_note`, injected into the system prompt), task history.
* SQLite migrations (v1 → v2) with an online backup/restore API.
* Provider router with per-provider cooldown, `!model` switch, `!models` discovery.
* Secret redaction across logs, tool results, Discord output and provider errors.
* 299 pytest tests: workspace escapes, symlink escapes, command matrix, fallback,
  Gemini model selection, blocked responses, approval flow, health endpoint, and a
  local HTTP mock that exercises the real `google-genai` SDK and the Groq request
  path end to end.

## What still needs your credentials

These cannot be verified without your keys, and the 3.0 code does not pretend
otherwise:

* a live Gemini call (use `!testai`),
* a live Groq call (use `!testai`),
* a live Discord connection (bot token + Message Content Intent),
* a Render deploy,
* any GitHub push from the agent itself (`GITHUB_TOKEN` + `ALLOW_GIT_PUSH=true`).

## Migration steps

1. Back up the old database if you have one (`data/hermes.sqlite3`).
2. Replace `agent.py` with the new entry point and add the `hermes/` package.
3. `pip install -r requirements.txt` (this upgrades `google-genai`).
4. Keep `DATA_DIR` inside the project unless you have a real persistent disk.
5. Run `python agent.py --check`, then `python -m pytest -q`.
6. Deploy, then `!health`, `!models`, `!testai`.
7. Old chat history is preserved: the v1 tables are the same names, and the v2
   migration only adds `project_memory`.
