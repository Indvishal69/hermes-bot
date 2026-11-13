# Rate limits and free tiers

Hermes calls an LLM several times per task — one call per step, up to
`MAX_AGENT_STEPS`. Free tiers are sized for chat, not for agent loops, so this page is
about what actually breaks and what to change.

## What the free tiers allow (November 2026)

**Groq** (`openai/gpt-oss-120b`, free/on-demand tier):

| Limit | Value |
| --- | --- |
| Tokens per minute (TPM) | 8,000 |
| Tokens per day | 200,000 (varies) |
| Error shape | `429 … Limit 8000, Used 7151, Requested 2068. Please try again in 9.1425s` |

**Gemini** (free tier, per project, resets midnight Pacific):

| Model | RPM | RPD |
| --- | --- | --- |
| `gemini-3.8-flash` | 5 | **20** |
| `gemini-3.6-flash` | 5 | **20** |
| `gemini-3.5-flash-lite` | 10–15 | **500** |
| `gemini-3.1-flash-lite` | 10–15 | **500** |

Both are per project, so extra API keys in the same project share the quota.

## Why an agent hits these fast

One step sends the system prompt (~1,000 tokens), the last `MAX_HISTORY_MESSAGES` turns
and the previous tool result (up to `MAX_OUTPUT_CHARS`), plus the completion budget. At
Groq's 8,000 TPM that is roughly **three or four steps per minute**. A task that needs
six steps will hit the ceiling mid-task.

## What Hermes does about it

1. **Honours the wait hint.** Groq and Gemini both say how long to wait (`Retry-After`,
   `retryDelay`, or "try again in 9.1425s"). If that is ≤ `RATE_LIMIT_WAIT_CAP`
   (default 20s), Hermes waits and retries the same provider — up to twice — instead of
   failing the task. A 9-second limit becomes a 9-second pause, not an error.
2. **Falls back instead of stalling** when the hint is longer than the cap, or when the
   other provider is healthy.
3. **Long quarantine for daily quotas.** A Gemini daily-quota 429 sets a
   `QUOTA_COOLDOWN` (default 3600s) so the bot stops re-trying a limit that cannot
   recover for hours. `!health` shows the cooldown and whether it was a daily quota.
4. **Keeps requests small**: `MAX_HISTORY_MESSAGES=10`, `MAX_COMPLETION_TOKENS=1500`,
   `MAX_OUTPUT_CHARS=4000`.
5. **Says what happened.** When everything is limited, the Discord reply names the limit,
   the suggested wait, and the knobs to change — with the raw provider error underneath.

## Tuning for a free tier

| Symptom | Change |
| --- | --- |
| Groq TPM 429 mid-task | `MAX_AGENT_STEPS=5`, `MAX_HISTORY_MESSAGES=6`, `MAX_OUTPUT_CHARS=2500` |
| Gemini "20 requests/day" | `GEMINI_MODEL=gemini-3.5-flash-lite` (500/day), or enable billing |
| Both limited at once | keep `ACTIVE_PROVIDER=groq`, `RATE_LIMIT_WAIT_CAP=20`, and send shorter tasks |
| Long tasks keep dying | split the task into two messages — each one restarts the token budget |

If you need uninterrupted agent work, there are three honest answers: a Groq Dev-tier
key, a billed Gemini project, or **OpenRouter** (`OPENROUTER_API_KEY`) — it fronts many
models, `openrouter/auto` picks one per request, and
`OPENROUTER_FALLBACK_MODELS=model-a,model-b` gives Hermes a chain to rotate through when
a model is gated or rate-limited.

## Reading the errors

```
429 … tokens per minute (TPM): Limit 8000 … try again in 9.1425s   → wait, then retry (automatic)
429 … generate_content_free_tier_requests, limit: 20, model: …     → daily cap; change model or wait for reset
429 … rate limit per minute                                         → burst; backoff handles it
503 UNAVAILABLE … high demand                                       → Google-side capacity; fallback handles it
```

`!health` shows each provider's cooldown and last error; `!testai` calls each provider
once and prints the real error.
