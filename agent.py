import os
import json
import sqlite3
import asyncio
import subprocess
import threading
import requests
from flask import Flask
import discord

# 1. 24/7 Keep-Alive Web Server
app = Flask(__name__)

@app.route("/")
def home():
    return "Hermes Super-Advanced Agent 24/7 Online!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# 2. SQLite Database for Long-Term Memory (Remembers Vishal & past chats)
DB_FILE = "memory.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS memory 
                 (user_id TEXT, role TEXT, content TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS connectors 
                 (key TEXT PRIMARY KEY, value TEXT)''')
    conn.commit()
    conn.close()

init_db()

def load_connectors_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT key, value FROM connectors")
    rows = c.fetchall()
    conn.close()
    data = {}
    for k, v in rows:
        os.environ[k] = v
        data[k] = v
    return data

def save_connector_db(key, value):
    os.environ[key] = str(value)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("REPLACE INTO connectors (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

load_connectors_db()

def get_chat_history(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT role, content FROM memory WHERE user_id = ? ORDER BY rowid DESC LIMIT 10", (str(user_id),))
    rows = c.fetchall()
    conn.close()
    history = []
    for role, content in reversed(rows):
        history.append({"role": role, "content": content})
    return history

def save_chat_history(user_id, role, content):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO memory (user_id, role, content) VALUES (?, ?, ?)", (str(user_id), role, content))
    conn.commit()
    conn.close()

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
ACTIVE_PROVIDER = os.environ.get("ACTIVE_PROVIDER", "groq")

# 3. AI Providers (Groq & Gemini Auto-Fallback)
def try_groq(messages, api_key):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    r = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=15)
    if r.status_code != 200:
        raise Exception(f"Groq Error ({r.status_code})")
    
    models = [m["id"] for m in r.json().get("data", [])]
    chat_models = [m for m in models if not any(s in m.lower() for s in ["whisper", "tts", "embed", "vision"])]
    
    priority = ["llama-3.3", "qwen", "deepseek", "llama-3.1", "70b", "mixtral", "gemma"]
    def score(m):
        for idx, p in enumerate(priority):
            if p in m.lower():
                return 100 - idx
        return 0
    chat_models.sort(key=score, reverse=True)
    
    for chosen in chat_models[:5]:
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json={"model": chosen, "messages": messages, "temperature": 0.3},
                timeout=45
            )
            if res.status_code == 200:
                return res.json()["choices"][0]["message"]["content"].strip(), f"groq ({chosen})"
        except Exception:
            continue
    raise Exception("All Groq models failed")

def try_gemini(messages, api_key):
    list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    r = requests.get(list_url, timeout=15)
    if r.status_code != 200:
        raise Exception(f"Gemini Key Error ({r.status_code})")
    
    all_m = r.json().get("models", [])
    valid = [m["name"] for m in all_m if "generateContent" in m.get("supportedGenerationMethods", []) and "gemini" in m["name"].lower()]
    
    prompt_text = "\n\n".join([f"{m['role'].upper()}:\n{m['content']}" for m in messages])
    for chosen in valid[:5]:
        url = f"https://generativelanguage.googleapis.com/v1beta/{chosen}:generateContent?key={api_key}"
        try:
            res = requests.post(url, json={"contents": [{"parts": [{"text": prompt_text}]}]}, timeout=45)
            if res.status_code == 200:
                return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip(), f"gemini ({chosen.replace('models/', '')})"
        except Exception:
            continue
    raise Exception("All Gemini models failed")

def call_ai(messages):
    global ACTIVE_PROVIDER
    providers = [ACTIVE_PROVIDER] + [p for p in ["groq", "gemini"] if p != ACTIVE_PROVIDER]
    errors = []
    
    for prov in providers:
        try:
            if prov == "groq" and os.environ.get("GROQ_API_KEY"):
                return try_groq(messages, os.environ.get("GROQ_API_KEY"))
            elif prov == "gemini" and os.environ.get("GEMINI_API_KEY"):
                return try_gemini(messages, os.environ.get("GEMINI_API_KEY"))
        except Exception as e:
            errors.append(f"{prov}: {str(e)}")
            
    return "❌ AI Error: " + " | ".join(errors), "none"

def run_terminal(command):
    try:
        res = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=120, env=os.environ.copy()
        )
        out = (res.stdout or "") + (res.stderr or "")
        return out[:1800] if out.strip() else "Executed successfully (no output)."
    except Exception as e:
        return f"Error: {str(e)}"

# 4. Discord Bot
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    print(f"Super-Advanced Hermes Online as {bot.user}")

@bot.event
async def on_message(message):
    global ACTIVE_PROVIDER
    if message.author == bot.user or message.author.id != ALLOWED_USER_ID:
        return

    text = message.content.strip()

    if text.startswith("!connect "):
        parts = text.split(" ", 2)
        if len(parts) < 3:
            await message.channel.send("⚠️ Format: `!connect KEY_NAME VALUE`")
            return
        key_name, key_val = parts[1].upper().strip(), parts[2].strip()
        save_connector_db(key_name, key_val)
        try:
            await message.delete()
        except Exception:
            pass
        await message.channel.send(f"✅ `{key_name}` successfully saved to database & environment!")
        return

    if text == "!connectors":
        keys = [k for k in os.environ.keys() if any(k.endswith(s) for s in ["_KEY", "_TOKEN", "_SECRET", "_ID"])]
        await message.channel.send(f"🔌 **Active Connected Keys:** `{', '.join(sorted(keys))}`")
        return

    # Save user message to memory database
    save_chat_history(message.author.id, "user", text)
    history = get_chat_history(message.author.id)

    active_tokens = [k for k in os.environ.keys() if k.endswith("_TOKEN") or k.endswith("_KEY")]
    sys_prompt = f"""You are Hermes, an elite autonomous full-stack Linux AI agent created for Vishal.
- Connected environment tokens: {', '.join(active_tokens)}
- You have persistent memory database. You know the user's name is Vishal.
- When you need to run a Linux terminal command, reply with ONLY: RUN_CMD: <command>
- When you need to send a generated file to Discord, reply with ONLY: SEND_FILE: <filepath>
- Do NOT wrap commands in markdown code blocks.
- Reply cleanly and professionally."""

    messages = [{"role": "system", "content": sys_prompt}] + history
    status_msg = await message.channel.send("⚙️ *Thinking...*")

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
                messages.append({"role": "user", "content": f"File {filepath} sent."})
            else:
                messages.append({"role": "user", "content": f"Error: File not found."})
        else:
            save_chat_history(message.author.id, "assistant", clean_reply)
            await status_msg.edit(content=f"✅ **Done (via `{used_prov}`):**\n{clean_reply[:1900]}")
            break

bot.open_db = init_db
bot.run(DISCORD_TOKEN)
