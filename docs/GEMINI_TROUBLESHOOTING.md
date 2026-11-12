# Gemini troubleshooting

## Why the 2.0 Gemini integration failed

Five separate problems, all visible in the old `agent.py`:

1. **Wrong SDK pin.** `requirements.txt` pinned `google-genai>=1.30,<2`. The current
   SDK is 3.x; 1.x predates the current model families and endpoint behaviour.
2. **Hardcoded model ID with no discovery.** The spec asked for model availability
   checks; the code called `generate_content(model=GEMINI_MODEL)` and hoped. When a
   model ID is retired or not enabled for a key, the call fails with `404 NOT_FOUND`
   and there was no fallback.
3. **Conversation flattened to one string.** All turns were concatenated into
   `USER: ... / ASSISTANT: ...` text and sent as a single `contents` string. Multi-turn
   structure, and any tool/JSON protocol relying on roles, degraded immediately.
4. **`response.text` assumed to exist.** On a safety block or empty candidate,
   `response.text` is `None`/raises, and the code turned that into
   "Gemini returned no text. Check model access or safety/block status." — the actual
   `finish_reason` was thrown away.
5. **No retries, and only partial redaction.** A 429 (quota) failed the whole task
   immediately, and only `GEMINI_API_KEY` was scrubbed from error text — other
   credential shapes in a response body were not.

## What 3.0 does instead

`hermes/providers/gemini.py`:

| Concern | Behaviour |
| --- | --- |
| Model choice | `client.models.list()` → filter to text models (`generateContent`, no `tts`/`live`/`image`/`embedding`/`transcribe`/`computer-use`) → prefer your `GEMINI_MODEL` if present, else `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-flash-latest`, `gemini-3.1-pro-preview`, else any `flash` model |
| Retired model | A `404`/`NOT_FOUND` triggers one re-discovery and one retry with the newly selected model |
| Context | Turns become `contents` with roles `user`/`model`; consecutive same-role turns are merged; a leading model turn is preceded by a user turn |
| System prompt | Passed as `system_instruction`, never as a fake user message |
| Quota (429) | Classified `quota`, retried up to 3 times with exponential backoff (1.5s, 2.25s, …), then the router falls back to Groq |
| Auth (401/403) | Classified `auth`, **not** retried, reported verbatim, provider quarantined for 5 minutes |
| Blocked/empty | Reported with `finish_reason`, `prompt_feedback.block_reason` and flagged safety categories |
| Secrets | Every error string passes through `redact()` which removes known secret values plus Google/Groq/GitHub/HF key shapes |

## Diagnosing with the bot

```
!testai     # one call per provider, with the real error text
!models     # what your key can actually use (Groq + Gemini)
!health     # includes the last Gemini error and the model-list error
```

`!testai` output maps directly onto the table below.

## Error → cause → fix

| What you see | Real cause | Fix |
| --- | --- | --- |
| `[auth HTTP 401] API key not valid` | Key wrong, revoked, or from another project | Regenerate in Google AI Studio, update `GEMINI_API_KEY` on the host, redeploy |
| `[auth HTTP 403] PERMISSION_DENIED: Generative Language API has not been used in project ... or it is disabled` | Gemini API not enabled for the project | Enable "Generative Language API" in Google Cloud console for that project, wait 1–2 min |
| `[model HTTP 404] model gemini-x is not found for API version v1beta` | Model ID retired or not available to your key | Leave `GEMINI_MODEL` empty (auto-discovery), or run `!models` and pin one from the list |
| `[quota HTTP 429] RESOURCE_EXHAUSTED` | Free-tier rate limit / daily quota | Wait, or rely on the Groq fallback; check quota in AI Studio |
| `no usable text model (no models reported by the API)` | Key cannot list models (bad key, disabled API, region) | Fix key/API first; `!models` shows the underlying listing error |
| `no text returned — finish_reason=SAFETY` | Prompt/response blocked by safety filters | Rephrase; this is a content policy result, not a bug |
| `no text returned — finish_reason=MAX_TOKENS` | `MAX_COMPLETION_TOKENS` too small for the reply | Raise `MAX_COMPLETION_TOKENS` (e.g. 4000) |
| `ClientError: 404 NOT FOUND` from model listing | Wrong `GEMINI_API_BASE` (proxy path) | Clear `GEMINI_API_BASE` unless you really use a proxy |
| Everything works in `!testai` but tasks fail | Groq is the preferred provider and is failing | `!model gemini`, or fix the Groq error shown in `!health` |

## Checking from outside the bot

```bash
export GEMINI_API_KEY=...
python - <<'PY'
from google import genai
c = genai.Client(api_key=__import__("os").environ["GEMINI_API_KEY"])
names = [m.name for m in c.models.list()]
print(len(names), "models visible")
print([n for n in names if "flash" in n][:10])
r = c.models.generate_content(
    model="gemini-3.8-flash",
    contents=[{"role": "user", "parts": [{"text": "Reply with OK"}]}],
    config={"system_instruction": "Be terse.", "temperature": 0},
)
print(r.text)
PY
```

If this works but the bot does not, the difference is configuration: compare the
bot's `!health` output (model, last error) with the model you just called.

## Model IDs change

Gemini retires model IDs on a schedule (`gemini-2.5-flash` and `gemini-2.5-pro` are
deprecated, with API traffic routed to newer models from 1 December 2026). That is
exactly why Hermes 3.0 does not hardcode one: leave `GEMINI_MODEL` empty and the bot
picks a model that your key can actually use today. If you pin a model, pin one that
`!models` lists, and re-check after Google announces a deprecation.

## Never do this

* Do not paste keys into Discord, screenshots, commits or `.env.example`.
* Do not log `os.environ` or the request headers while debugging — Hermes redacts
  known secrets, but a screenshot does not.
* Do not disable the approval gate or the command allowlist to "make it work".
  Gemini is not the reason those exist.
