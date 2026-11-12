# Test report — Hermes 3.0

Everything below was executed in a Linux sandbox (Debian 12, x86_64) on
12 November 2026. Nothing here is a projection; where a test needed real
credentials, it is listed as **not run** instead of assumed.

## Environment

| Item | Value |
| --- | --- |
| Python | 3.12.13 |
| google-genai | 3.2.0 |
| discord.py | 2.7.1 |
| Flask | 3.1.3 |
| requests | 2.34.2 |
| pytest | installed for the run, no network access needed |

## 1. Syntax and import checks

```
$ python -m compileall -q hermes agent.py
syntax OK
```

All modules import, and the Discord client constructs with dummy credentials
(`HermesBot` instantiated, all command handlers present).

## 2. Automated test suite

```
$ python -m pytest
.....................................................................    [100%]
235 passed in 13.65s
```

Coverage by area (all passing):

| Area | What is actually asserted |
| --- | --- |
| `tests/test_config.py` (11) | required vars reported together; non-numeric owner id; invalid `ACTIVE_PROVIDER` falls back; provider without key switches; bad integers use defaults; secrets list; redacted summary contains no keys; `DATA_DIR` fallback when the configured path is unwritable |
| `tests/test_db.py` (11) | migrations idempotent; v1→v2 upgrade keeps data; per-user trimming; history limit/clear; notes; task lifecycle; audit ok/failed; kv expiry; online backup + restore roundtrip; restore refuses a foreign file; stats |
| `tests/test_files.py` (20) | write/read; Created vs Updated; `../`, absolute, nested-escape and NUL paths rejected; symlink escape rejected; size limit; `.git` writes refused; line windows; replace; delete rules; list/tree; search by glob; zip contents; workspace info |
| `tests/test_shell.py` (42) | 11 legitimate commands allowed (incl. `pip install python-dotenv` — a 2.0 false positive); 20 dangerous commands rejected (`rm -rf /`, `sudo`, `curl`, `bash -c`, `&&`, pipes, `>`, `os.environ`, `.env`, `git push`, `git reset --hard`, absolute paths, …); `git push` only with `ALLOW_GIT_PUSH`; force-push always blocked; real execution with exit codes; timeout kills the process; rejected command never runs; **child process cannot see `DISCORD_TOKEN`/`GROQ_API_KEY`**; syntax checker; process registry |
| `tests/test_router.py` (11) | preference order; fallback on failure; cooldown ordering; all-fail reports every error; unconfigured skipped; no-provider error; `prefer=`; success clears error; unexpected exception wrapped; secrets redacted from recorded errors; duplicate order entries de-duplicated |
| `tests/test_gemini.py` (26) | name normalisation; text-model filtering (tts/live/image/embedding/transcribe/computer-use excluded, `supportedActions` respected); configured-model preference; fallback to preferred; auto-selection; flash preference; empty-list behaviour; **roles preserved** (`user/model/user`), consecutive turns merged, `system_instruction` passed; retired model triggers re-discovery + retry; auth error not retried; quota classified retryable; `finish_reason=SAFETY` reported as blocked; listing error surfaced |
| `tests/test_loop.py` (16) | write-then-final; tool result fed back; **command requires approval and does not execute**; resume after approval; blocked command never offered for approval; invalid JSON repaired once; fenced JSON accepted; provider failure reported honestly; step limit yields an honest summary; model path escape rejected; secrets redacted from reported errors; conversation history reused; project memory persisted |
| `tests/test_groq.py` (26) | chat-model filtering (whisper/tts/orpheus/guard excluded); configured model honoured; **retired ID replaced by `openai/gpt-oss-120b` and the call retried**; auto-selection; only-non-chat models rejected; empty list keeps the configured ID; default model is not a retired ID; listing cache; 429 retried; 401 not retried; payload uses `max_completion_tokens` and a real system message; empty message reports `finish_reason`; listing failure surfaced without blocking |
| `tests/test_connectors.py` (41) | input validation (`safe_id`/`safe_path`/`safe_repo` reject traversal, spaces, hosts); HttpClient refuses absolute URLs and adds hints on 401/404; registry: active-by-default, missing env, disable/enable persistence across a restart, settings validation and persistence, tool-spec approval flags, secret redaction in errors; real connectors against fake HTTP (GitHub repo/file/issue, Notion page-URL parsing and paragraph blocks, Slack `ok:false`, Cloudflare purge body, Render deploy URL) |
| `tests/test_bot.py` (21) | Discord layer: no shadowing of `discord.Client` attributes; **a task survives discord.py overwriting `bot.loop` at startup**; progress edits then finish; unauthorized users ignored and audited; bot messages ignored; `!help`; unknown `!command` becomes a task; task error surfaced; rate limit; approval prompt contents; `!deny` resumes without running; provider models resolved at startup without a failure stopping the boot |
| `tests/test_web_and_utils.py` (10) | `/health` 200 + payload; **no paths or secrets in the response**; unhealthy database → 503; `/ready` and `/`; redaction of key shapes; JSON extraction (plain, fenced, embedded, malformed); backoff growth and cap; truncate/chunk; human helpers |

## 3. Preflight (`python agent.py --check`)

Healthy configuration:

```
Hermes 3.0 3.0.0 — preflight
  storage: /tmp/h3check writable
  storage: /tmp/h3check/workspace writable
  storage: /tmp/h3check/backups writable
  database: /tmp/h3check/hermes.sqlite3 ready (schema v2)
  provider groq: configured
  provider gemini: not configured
RESULT: OK
```

Empty environment — every problem reported at once, exit code 1:

```
  config: ALLOWED_USER_ID is required (your numeric Discord user ID).
  config: DISCORD_TOKEN is required.
  config: Set at least one of GROQ_API_KEY or GEMINI_API_KEY.
RESULT: PROBLEMS FOUND
```

## 4. Real `google-genai` SDK path, against a local mock API

This exercises the actual SDK (not a fake): model listing, selection, request
construction, system instruction, multi-turn contents, and auth header.

```
models listed: ['gemini-3.8-flash']
resolved: gemini-3.8-flash | auto-selected gemini-3.8-flash
completion: mock reply model=gemini-3.8-flash system=yes turns=3
generate calls: 1 auth header present: True
```

The mock advertised an embedding model, a TTS model and one text model; the
provider kept only the text model. The request carried the system instruction and
three turns (`user`, `model`, `user`), matching the input.

## 5. Groq request path, retry and fallback behaviour

Mock returned HTTP 429 on the first call and 200 on the second:

```
Groq attempt 1/3 failed ([quota HTTP 429] groq: HTTP 429: {"error":{"message":"rate limit"}});
retrying in 1.645s
models: ['llama-3.1-8b-instant', 'llama-3.3-70b-versatile']
completion: groq-mock-ok model=llama-3.3-70b-versatile msgs=2
calls (1 = retried after 429): 2
```

## 6. Flask health server over a real socket

```
/health 200 {"database": true, "ok": true, "providers_configured": ["groq"], "service": "Hermes 3.0", "uptime_seconds": 1, "version": "3.0.0", "workspace_writable": true}
/ready  200 {"ready": true, "uptime_seconds": 1}
/       200 {"service": "Hermes 3.0", "status": "online", "uptime_seconds": 1, "version": "3.0.0"}
```

(`providers_configured` now contains unique names — a duplicate entry found during
this run was fixed and covered by `test_duplicate_names_in_order_are_deduplicated`.)

## 7. Not run — needs your credentials or your infrastructure

| Test | Why it was not run | How to run it |
| --- | --- | --- |
| Live Gemini completion | no real `GEMINI_API_KEY` here | `!testai` after deploy |
| Live Groq completion | no real `GROQ_API_KEY` here | `!testai` after deploy |
| Discord gateway connection and command handling | needs a bot token and Message Content Intent enabled | `!help`, `!health`, then a task |
| Render deploy and health check | needs your Render account | `docs/DEPLOYMENT.md` |
| GitHub push from the agent | needs `GITHUB_TOKEN` + `ALLOW_GIT_PUSH=true` | set both, then ask the agent |

The 3.0 code reports these states as "not configured" rather than pretending they
were verified.

## Reproducing

```bash
pip install -r requirements.txt pytest
python -m pytest
python agent.py --check
```

## 8. Regression found in production, then fixed (3.0.1)

Your screenshot from the live deployment showed every message ending in:

```
❌ Internal error: AttributeError: '_UnixSelectorEventLoop' object has no attribute 'handle'
```

Root cause: `discord.Client` owns a `loop` attribute, and
`Client._async_setup_hook()` (run inside `bot.run()`) replaces it with the running
asyncio loop. The 3.0.0 bot stored the `AgentLoop` as `self.loop`, so
`self.loop.handle(...)` became `event_loop.handle(...)`.

Fix: the attribute is `self.agent_loop` everywhere in `hermes/bot.py`, with a comment
explaining why, plus 11 tests in `tests/test_bot.py`. The regression test reproduces
the production condition (`bot.loop = asyncio.get_running_loop()`), and I verified it
fails against the old code with the same message:

```
E   AttributeError: '_UnixSelectorEventLoop' object has no attribute 'handle'
hermes/bot.py:459: AttributeError
FAILED tests/test_bot.py::test_agent_loop_does_not_shadow_discord_client_attributes
FAILED tests/test_bot.py::test_task_survives_discord_overwriting_client_loop
```

and passes after the rename. One test also scans `discord.Client.__init__` and
`_async_setup_hook` for assigned attribute names and fails if the bot ever shadows
one of them again.

## 9. Second production finding: Groq model retired (3.0.2)

Your `!testai` screenshot showed:

```
❌ groq: [model HTTP 404] groq: HTTP 404: {"error":{"message":"The model
llama-3.3-70b-versatile does not exist or you do not have access to it.", ...}}
❌ gemini: timed out after 75s
```

Two separate causes:

1. **Groq retired the model.** `llama-3.3-70b-versatile` and `llama-3.1-8b-instant`
   were shut down for free and developer tiers on 16 August 2026; Groq's published
   replacement is `openai/gpt-oss-120b`. Hermes 3.0.0/3.0.1 shipped that retired ID as
   the default and, unlike Gemini, did not check model availability first.
2. **A 503 burst outlived the probe.** Gemini returned `503 UNAVAILABLE — This model
   is currently experiencing high demand`. That is retryable and was retried, but each
   attempt had no HTTP timeout of its own, so three attempts ran past the 75s probe
   limit and surfaced as a bare "timed out".

Fixes:

* `GroqProvider.resolve_model()` lists `GET /openai/v1/models`, keeps the configured ID
  only if it is really there, otherwise picks `openai/gpt-oss-120b` (or the first
  preferred chat model the key can use), and re-discovers once on `404 model_not_found`.
* Default `GROQ_MODEL` is now `openai/gpt-oss-120b`; a stale value in your Render env
  is corrected automatically at runtime instead of failing every message.
* Both providers now cap each HTTP attempt (50s default, 30s in `!testai`), so retries
  fit inside `AI_TIMEOUT`; a timeout message now includes the last provider error.
* Model selection is shared by both providers (`providers/base.py::choose_model`).

Verified against local mock APIs, end to end:

```
reply: live-ok model=openai/gpt-oss-120b
model after recovery: openai/gpt-oss-120b
models endpoint hits: 1 | chat attempts: ['llama-3.3-70b-versatile', 'openai/gpt-oss-120b']
```

and the per-attempt timeout is enforced rather than assumed — a server that sleeps
20s is abandoned at the configured bound:

```
raised after 10.0s: ProviderError
```

Gemini still completes through the real SDK with the new per-request timeout
(`gemini reply: gemini-ok ...`).


## 10. Connector system (3.1.0)

Verified end to end with fake credentials in a real registry, real database and the
real bot object:

```
github       configured=True  enabled=True  tools=9 missing=[]
notion       configured=True  enabled=True  tools=4 missing=[]
slack        configured=False enabled=False tools=2 missing=['SLACK_BOT_TOKEN']
...
active tools: 13

**Connectors** — 2/7 connected
• **github** — connected (9 tools)
• **slack** — not configured — needs SLACK_BOT_TOKEN
```

What that proves:

* Only configured connectors expose tools (13 tools from 2 connectors, not 20 from 7).
* Missing credentials are reported **by variable name**, never by value.
* The agent prompt lists exactly those tools, with `[OWNER APPROVAL REQUIRED]` on the
  write ones (`github_create_issue`, `github_commit_file`, `notion_append_text`, …).
* Disabling survives a restart: after `disable("notion")` a freshly built registry
  reports `notion active: False` and exposes 9 tools (GitHub only) — the state is in
  SQLite, not in memory.
* A write call produces an approval payload and does not run:
  `requires_approval: True | kind: connector | tool: github_create_issue |
  args: {'title': 't', 'body': 'b', 'repo': 'a/b'}`, and the audit row is
  `connector_requested`.
* `reason` is stripped from tool arguments before the call (it explains the request to
  you; passing it through would have failed validation).
