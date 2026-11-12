import os
import json
import asyncio
import subprocess
import threading
import requests
from flask import Flask
import discord

# 1. Keep-Alive Web Server for Render 24/7
app = Flask(__name__)

@app.route("/")
def home():
    return "Hermes Super-Agent 24/7 Active!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# 2. Dynamic Connector & Key Storage
CONFIG_FILE = "connectors.json"

def load_connectors():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            for k, v in data.items():
                os.environ[k] = v
            return data
    return {}

def save_connector(key, value):
    data = load_connectors()
    data[key] = value
    os.environ[key] = value
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

load_connectors()

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
ACTIVE_PROVIDER = os.environ.get("ACTIVE_PROVIDER", "gemini")

# 3. Smart Auto-Detect Gemini Caller (Zero 404 Error)
def call_gemini(messages, api_key):
    list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    r = requests.get(list_url, timeout=15)
    if r.status_code != 200:
        raise Exception(f"Key error ({r.status_code}): {r.text[:120]}")

    all_models = r.json().get("models", [])
    valid_models = []
    skip_words = ["embedding", "imagen", "veo", "tts", "audio", "vision", "aqa", "bisheng"]
    for m in all_models:
        name = m.get("name", "")
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" in methods and "gemini" in name:
            if not any(w in name.lower() for w in skip_words):
                valid_models.append(name)

    # Best flash/pro model chunna
    chosen = None
    for pref in ["flash", "pro"]:
        for vm in valid_models:
            if pref in vm:
                chosen = vm
                break
        if chosen:
            break
    if not chosen and valid_models:
        chosen = valid_models[0]
    if not chosen:
        raise Exception("No text model found for this Gemini key.")

    prompt_text = "\n\n".join([f"{m['role'].upper()}:\n{m['content']}" for m in messages])
    gen_url = f"https://generativelanguage.googleapis.com/v1beta/{chosen}:generateContent?key={api_key}"
    res = requests.post(
        gen_url,
        json={"contents": [{"parts": [{"text": prompt_text}]}]},
        timeout=60
    )
    if res.status_code != 200:
        raise Exception(f"{chosen} ({res.status_code}): {res.text[:120]}")
    data = res.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip(), chosen.replace("models/", "")

# 4. Smart Auto-Detect Groq Caller (Zero 404 Error)
def call_groq(messages, api_key):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    r = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=15)
    if r.status_code != 200:
        raise Exception(f"Groq key error ({r.status_code}): {r.text[:120]}")

    models = [m["id"] for m in r.json().get("data", [])]
    skip = ["whisper", "tts", "guard", "playai", "orpheus", "embed"]
    chat_models = [m for m in models if not any(s in m.lower() for s in skip)]

    chosen = None
    for pref in ["70b", "llama", "qwen", "deepseek", "gemma", "mixtral"]:
        for cm in chat_models:
            if pref in cm.lower():
                chosen = cm
                break
        if chosen:
            break
    if not chosen and chat_models:
        chosen = chat_models[0]

    res = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers,
        json={"model": chosen, "messages": messages, "temperature": 0.2},
        timeout=60
    )
    if res.status_code != 200:
        raise Exception(f"{chosen} ({res.status_code}): {res.text[:120]}")
    return res.json()["choices"][0]["message"]["content"].strip(), f"groq/{chosen}"

def call_ai(messages):
    global ACTIVE_PROVIDER
    order = ["gemini", "groq"] if ACTIVE_PROVIDER == "gemini" else ["groq", "gemini"]
    errors = []

    for prov in order:
        try:
            if prov == "gemini" and os.environ.get("GEMINI_API_KEY"):
                return call_gemini(messages, os.environ.get("GEMINI_API_KEY"))
            elif prov == "groq" and os.environ.get("GROQ_API_KEY"):
                return call_groq(messages, os.environ.get("GROQ_API_KEY"))
        except Exception as e:
            errors.append(f"{prov}: {str(e)}")

    return "❌ AI Error:\n" + ("\n".join(errors) if errors else "No API key found. Send `!connect GEMINI_API_KEY your_key`"), "none"

def run_terminal(command):
    try:
        res = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=120, env=os.environ.copy()
        )
        out = (res.stdout or "") + (res.stderr or "")
        return out[:1800] if out else "Executed successfully (no output)."
    except Exception as e:
        return f"Error: {str(e)}"

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    print(f"Super-Agent Online as {bot.user}")

@bot.event
async def on_message(message):
    global ACTIVE_PROVIDER
    if message.author == bot.user or message.author.id != ALLOWED_USER_ID:
        return

    text = message.content.strip()

    if text.startswith("!connect "):
        parts = text.split(" ", 2)
        if len(parts) < 3:
            await message.channel.send("⚠️ Usage: `!connect KEY_NAME YOUR_TOKEN`")
            return
        key_name, key_val = parts[1].upper(), parts[2].strip()
        save_connector(key_name, key_val)
        try:
            await message.delete()
        except Exception:
            pass
        await message.channel.send(f"✅ `{key_name}` connected successfully!")
        return

    if text == "!connectors":
        custom_keys = [k for k in os.environ.keys() if k.endswith("_TOKEN") or k.endswith("_KEY")]
        await message.channel.send(
            f"🔌 **Active Keys:** `{', '.join(custom_keys)}`\n"
            f"🧠 **Preferred Brain:** `{ACTIVE_PROVIDER}`"
        )
        return

    if text.startswith("!model "):
        choice = text.split(" ", 1)[1].strip().lower()
        if choice in ["gemini", "groq"]:
            ACTIVE_PROVIDER = choice
            save_connector("ACTIVE_PROVIDER", choice)
            await message.channel.send(f"🧠 Preferred brain switched to **{choice}** (auto-model selection enabled)!")
        else:
            await message.channel.send("⚠️ Choose: `!model gemini` or `!model groq`")
        return

    active_tokens = [k for k in os.environ.keys() if k.endswith("_TOKEN") or k.endswith("_KEY")]
    sys_prompt = f"""You are Hermes, an autonomous full-stack Linux AI agent.
Available environment tokens in $ENV: {', '.join(active_tokens)}
- Use connected tokens directly in bash commands (e.g., $VERCEL_TOKEN, $HF_TOKEN, $GITHUB_TOKEN) via curl, python, git, or CLI.
- To run a bash command, reply ONLY with: RUN_CMD: <command>
- To send a file to Discord, reply ONLY with: SEND_FILE: <filepath>
- Otherwise, reply with the final answer cleanly."""

    status_msg = await message.channel.send(f"⚙️ *Thinking ({ACTIVE_PROVIDER})...*")
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": text}
    ]

    for _ in range(8):
        reply, used_prov = await asyncio.to_thread(call_ai, messages)

        if reply.startswith("RUN_CMD:"):
            cmd = reply.replace("RUN_CMD:", "").strip()
            await message.channel.send(f"💻 `{cmd[:150]}`")
            output = await asyncio.to_thread(run_terminal, cmd)
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": f"Terminal Output:\n{output}"})
        elif reply.startswith("SEND_FILE:"):
            filepath = reply.replace("SEND_FILE:", "").strip()
            if os.path.exists(filepath):
                await message.channel.send(file=discord.File(filepath))
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": "File sent."})
            else:
                messages.append({"role": "user", "content": "Error: File not found."})
        else:
            await status_msg.edit(content=f"✅ **Done (via `{used_prov}`):**\n{reply[:1900]}")
            break

bot.run(DISCORD_TOKEN)
