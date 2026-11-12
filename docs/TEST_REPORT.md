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
143 passed in 19.76s
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
| `tests/test_loop.py` (12) | write-then-final; tool result fed back; **command requires approval and does not execute**; resume after approval; blocked command never offered for approval; invalid JSON repaired once; fenced JSON accepted; provider failure reported honestly; step limit yields an honest summary; model path escape rejected; secrets redacted from reported errors; conversation history reused; project memory persisted |
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
