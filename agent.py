import os
import json
import asyncio
import subprocess
import threading
import requests
from flask import Flask
import discord

# 1. Keep-Alive Web Server (24/7 Render Support)
app = Flask(__name__)

@app.route("/")
def home():
    return "Hermes Super-Agent 24/7 Online (Auto-Model Router Active)!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# 2. Dynamic Connectors & API Keys Storage
CONFIG_FILE = "connectors.json"

def load_connectors():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                for k, v in data.items():
                    os.environ[k] = str(v)
                return data
        except Exception:
            return {}
    return {}

def save_connector(key, value):
    data = load_connectors()
    data[key] = value
    os.environ[key] = str(value)
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

load_connectors()

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
ACTIVE_PROVIDER = os.environ.get("ACTIVE_PROVIDER", "auto")

# 3. Provider 1: Smart Gemini Auto-Fallback Loop
def try_gemini(messages, api_key):
    list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    r = requests.get(list_url, timeout=15)
    if r.status_code != 200:
        raise Exception(f"Key invalid ({r.status_code})")

    all_models = r.json().get("models", [])
    skip_words = ["embedding", "imagen", "veo", "tts", "audio", "vision", "aqa", "bisheng", "2.5-flash"]

    candidates = []
    for m in all_models:
        name = m.get("name", "")
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" in methods and "gemini" in name.lower():
            if not any(w in name.lower() for w in skip_words):
                candidates.append(name)

    # Sort models so latest working flash/pro models are tried first
    def score_model(m_name):
        s = 0
        if "flash" in m_name:
            s += 20
        if "pro" in m_name:
            s += 10
        if "latest" in m_name or "2.0" in m_name or "1.5" in m_name or "3." in m_name:
            s += 15
        return s

    candidates.sort(key=score_model, reverse=True)
    prompt_text = "\n\n".join([f"{m['role'].upper()}:\n{m['content']}" for m in messages])
    last_err = "No active Gemini model responded"

    # Har model ko ek-ek karke try karega jab tak 200 OK na mile
    for model_name in candidates[:8]:
        url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:generateContent?key={api_key}"
        try:
            res = requests.post(
                url,
                json={"contents": [{"parts": [{"text": prompt_text}]}]},
                timeout=45
            )
            if res.status_code == 200:
                data = res.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                return text, f"gemini ({model_name.replace('models/', '')})"
            last_err = f"{model_name.replace('models/', '')} [{res.status_code}]"
        except Exception as e:
            last_err = str(e)[:80]
            continue

    raise Exception(last_err)

# 4. Provider 2: Smart Groq Auto-Fallback Loop
def try_groq(messages, api_key):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    r = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=15)
    if r.status_code != 200:
        raise Exception(f"Groq Key Error ({r.status_code})")

    raw_models = [m["id"] for m in r.json().get("data", [])]
    skip = ["whisper", "tts", "guard", "playai", "orpheus", "embed", "vision"]
    chat_models = [m for m in raw_models if not any(s in m.lower() for s in skip)]

    # Best coding/agent models ko priority dena
    priority = ["llama-3.3", "qwen", "deepseek", "llama-3.1", "70b", "mixtral", "gemma", "8b"]
    def score_groq(m_id):
        for idx, p in enumerate(priority):
            if p in m_id.lower():
                return 100 - idx
        return 0

    chat_models.sort(key=score_groq, reverse=True)
    last_err = "No Groq model available"

    for chosen in chat_models[:6]:
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json={"model": chosen, "messages": messages, "temperature": 0.2},
                timeout=45
            )
            if res.status_code == 200:
                text = res.json()["choices"][0]["message"]["content"].strip()
                return text, f"groq ({chosen})"
            last_err = f"{chosen} [{res.status_code}]"
        except Exception as e:
            last_err = str(e)[:80]
            continue

    raise Exception(last_err)

# 5. Provider 3: Smart OpenRouter Free Models Auto-Loop
def try_openrouter(messages, api_key):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    fallback_free = [
        "openrouter/free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "qwen/qwen-2.5-coder-32b-instruct:free",
        "deepseek/deepseek-chat:free",
        "google/gemma-3-27b-it:free",
        "mistralai/mistral-small-3.1-24b-instruct:free"
    ]

    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        if r.status_code == 200:
            dyn_free = [m["id"] for m in r.json().get("data", []) if m["id"].endswith(":free")]
            for dm in dyn_free[:6]:
                if dm not in fallback_free:
                    fallback_free.append(dm)
    except Exception:
        pass

    last_err = "No OpenRouter free model responded"
    for chosen in fallback_free:
        try:
            res = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json={"model": chosen, "messages": messages, "temperature": 0.2},
                timeout=45
            )
            if res.status_code == 200:
                data = res.json()
                if "choices" in data and data["choices"]:
                    text = data["choices"][0]["message"]["content"].strip()
                    return text, f"openrouter ({chosen})"
            last_err = f"{chosen} [{res.status_code}]"
        except Exception as e:
            last_err = str(e)[:80]
            continue

    raise Exception(last_err)

# 6. Master AI Router (Auto-Switches Providers & Models)
def call_ai(messages):
    global ACTIVE_PROVIDER
    all_providers = ["groq", "gemini", "openrouter"]

    if ACTIVE_PROVIDER in all_providers:
        order = [ACTIVE_PROVIDER] + [p for p in all_providers if p != ACTIVE_PROVIDER]
    else:
        order = all_providers

    errors = []
    for prov in order:
        try:
            if prov == "groq" and os.environ.get("GROQ_API_KEY"):
                return try_groq(messages, os.environ.get("GROQ_API_KEY"))
            elif prov == "gemini" and os.environ.get("GEMINI_API_KEY"):
                return try_gemini(messages, os.environ.get("GEMINI_API_KEY"))
            elif prov == "openrouter" and os.environ.get("OPENROUTER_API_KEY"):
                return try_openrouter(messages, os.environ.get("OPENROUTER_API_KEY"))
        except Exception as e:
            errors.append(f"• {prov}: {str(e)}")
            continue

    if not errors:
        return "❌ Koi bhi API key set nahi hai! Send: `!connect GROQ_API_KEY gsk_...`", "none"
    return "❌ Sabhi keys/models fail hue:\n" + "\n".join(errors), "none"

# 7. Linux Terminal Runner
def run_terminal(command):
    try:
        res = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=120, env=os.environ.copy()
        )
        out = (res.stdout or "") + (res.stderr or "")
        return out[:1800] if out.strip() else "Executed successfully (no output)."
    except Exception as e:
        return f"Error: {str(e)}"

# 8. Discord Bot Interface
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    print(f"Hermes Super-Agent Ready as {bot.user}")

@bot.event
async def on_message(message):
    global ACTIVE_PROVIDER
    if message.author == bot.user or message.author.id != ALLOWED_USER_ID:
        return

    text = message.content.strip()

    # Command: !connect KEY VALUE
    if text.startswith("!connect "):
        parts = text.split(" ", 2)
        if len(parts) < 3:
            await message.channel.send("⚠️ Format: `!connect KEY_NAME YOUR_KEY_VALUE`")
            return
        key_name, key_val = parts[1].upper().strip(), parts[2].strip()
        save_connector(key_name, key_val)
        try:
            await message.delete()
        except Exception:
            pass
        await message.channel.send(f"✅ `{key_name}` save ho gaya aur Linux terminal mein active hai!")
        return

    # Command: !connectors
    if text == "!connectors":
        keys = [k for k in os.environ.keys() if any(k.endswith(s) for s in ["_KEY", "_TOKEN", "_SECRET", "_ID"])]
        await message.channel.send(
            f"🔌 **Connected Keys:** `{', '.join(sorted(keys))}`\n"
            f"🧠 **Brain Mode:** `{ACTIVE_PROVIDER}` (Auto-fallback ON)"
        )
        return

    # Command: !model <auto|groq|gemini|openrouter>
    if text.startswith("!model "):
        choice = text.split(" ", 1)[1].strip().lower()
        if choice in ["auto", "groq", "gemini", "openrouter"]:
            ACTIVE_PROVIDER = choice
            save_connector("ACTIVE_PROVIDER", choice)
            await message.channel.send(f"🧠 Brain mode set to **{choice.upper()}** (Auto-model fallback active)!")
        else:
            await message.channel.send("⚠️ Choose: `!model auto`, `!model groq`, `!model gemini`, or `!model openrouter`")
        return

    active_tokens = [k for k in os.environ.keys() if k.endswith("_TOKEN") or k.endswith("_KEY")]
    sys_prompt = f"""You are Hermes, an autonomous full-stack Linux AI agent running on a 24/7 server.
Connected environment variables available in bash: {', '.join(active_tokens)}
- You can use connected tokens directly in terminal commands (e.g., $VERCEL_TOKEN, $HF_TOKEN, $GITHUB_TOKEN) via curl, git, python, or CLI.
- When you need to execute a Linux shell command, reply with ONLY: RUN_CMD: <your command>
- When you need to upload/send a file to the user on Discord, reply with ONLY: SEND_FILE: <filepath>
- Do NOT wrap RUN_CMD: or SEND_FILE: in markdown code blocks.
- Once your task is complete or for normal conversation, reply normally and concisely."""

    status_msg = await message.channel.send(f"⚙️ *Thinking ({ACTIVE_PROVIDER})...*")
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": text}
    ]

    for _ in range(8):
        reply, used_prov = await asyncio.to_thread(call_ai, messages)
        clean_reply = reply.strip()

        if clean_reply.startswith("RUN_CMD:"):
            cmd = clean_reply.replace("RUN_CMD:", "", 1).strip()
            await message.channel.send(f"💻 `{cmd[:180]}`")
            output = await asyncio.to_thread(run_terminal, cmd)
            messages.append({"role": "assistant", "content": clean_reply})
            messages.append({"role": "user", "content": f"Terminal Output:\n{output}"})
        elif clean_reply.startswith("SEND_FILE:"):
            filepath = clean_reply.replace("SEND_FILE:", "", 1).strip()
            if os.path.exists(filepath):
                await message.channel.send(file=discord.File(filepath))
                messages.append({"role": "assistant", "content": clean_reply})
                messages.append({"role": "user", "content": f"File {filepath} sent to Discord."})
            else:
                messages.append({"role": "user", "content": f"Error: File {filepath} does not exist."})
        else:
            await status_msg.edit(content=f"✅ **Done (via `{used_prov}`):**\n{clean_reply[:1900]}")
            break

bot.run(DISCORD_TOKEN)
