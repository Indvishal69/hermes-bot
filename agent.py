import os
import json
import asyncio
import subprocess
import threading
from flask import Flask
import discord
from openai import OpenAI

app = Flask(__name__)

@app.route("/")
def home():
    return "Hermes Super-Agent 24/7 Active!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

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

PROVIDERS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "model": "gemini-2.5-flash"
    },
    "gemini-pro": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "model": "gemini-2.5-pro"
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile"
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "model": "deepseek/deepseek-chat:free"
    }
}

def call_ai(messages):
    global ACTIVE_PROVIDER
    order = [ACTIVE_PROVIDER] + [p for p in PROVIDERS if p != ACTIVE_PROVIDER]
    errors = []

    for prov_name in order:
        prov = PROVIDERS[prov_name]
        api_key = os.environ.get(prov["key_env"])
        if not api_key:
            continue
        try:
            client = OpenAI(api_key=api_key, base_url=prov["base_url"])
            resp = client.chat.completions.create(
                model=prov["model"],
                messages=messages,
                temperature=0.2
            )
            return resp.choices[0].message.content.strip(), prov_name
        except Exception as e:
            errors.append(f"{prov_name}: {str(e)[:100]}")
            continue

    return "❌ All AI keys failed or hit limits:\n" + "\n".join(errors), "none"

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
        await message.channel.send(f"✅ Connector `{key_name}` added to Linux environment!")
        return

    if text == "!connectors":
        custom_keys = [k for k in os.environ.keys() if k.endswith("_TOKEN") or k.endswith("_KEY")]
        await message.channel.send(
            f"🔌 **Active Connectors:** `{', '.join(custom_keys)}`\n"
            f"🧠 **Current Brain:** `{ACTIVE_PROVIDER}`"
        )
        return

    if text.startswith("!model "):
        choice = text.split(" ", 1)[1].strip().lower()
        if choice in PROVIDERS:
            ACTIVE_PROVIDER = choice
            save_connector("ACTIVE_PROVIDER", choice)
            await message.channel.send(f"🧠 Brain switched to **{choice}** (`{PROVIDERS[choice]['model']}`)!")
        else:
            await message.channel.send(f"⚠️ Available models: `{', '.join(PROVIDERS.keys())}`")
        return

    active_tokens = [k for k in os.environ.keys() if k.endswith("_TOKEN") or k.endswith("_KEY")]
    sys_prompt = f"""You are Hermes, an autonomous full-stack Linux AI agent.
Available environment tokens in $ENV: {', '.join(active_tokens)}
- Use connected tokens directly in bash commands (e.g., $VERCEL_TOKEN, $HF_TOKEN, $GITHUB_TOKEN) via curl, python, git, or CLI.
- To run a bash command, reply ONLY with: RUN_CMD: <command>
- To send a file to Discord, reply ONLY with: SEND_FILE: <filepath>
- Otherwise, reply with the final answer cleanly."""

    status_msg = await message.channel.send(f"⚙️ *Working ({ACTIVE_PROVIDER})...*")
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
