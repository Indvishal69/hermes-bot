# Deploying Hermes 3.0

Verified against Render's documented limits as of November 2026. Re-check
<https://render.com/docs/free> before you rely on any number here.

## The one thing to understand first

A Discord bot is an **outbound** WebSocket connection to Discord. Render's free web
services spin down after **15 minutes without inbound traffic** (HTTP requests or
WebSocket messages to *your* service), and a spun-down service takes about a minute
to wake. Nothing wakes it while it is idle — so a free Render web service running a
Discord bot will go offline after 15 idle minutes and stay offline until something
sends it an HTTP request.

Free-plan facts that matter here:

| Limit | Value |
| --- | --- |
| CPU / RAM | 0.1 CPU, 512 MB RAM, single instance |
| Idle | spins down after 15 min with no inbound traffic, ~1 min to wake |
| Filesystem | **ephemeral** — changes are lost on redeploy, restart and every spin-down |
| Persistent disk | **not available on free** (paid only, $0.25/GB/month) |
| Instance hours | 750 per workspace per calendar month |
| Shell/SSH | not available |
| Restarts | Render may restart a free service at any time |

Consequences for Hermes:

1. `DATA_DIR` must point **inside the project** on the free plan. The 2.0 blueprint
   used `DATA_DIR=/var/data` with no disk attached — that path is not writable and
   not persistent, so the app either failed to start or silently lost its database.
2. The SQLite database, workspace files and backups are **wiped** on every deploy,
   restart and spin-down. Download backups (`!backup`, then copy the file) before
   redeploying, or move to a paid plan.
3. The bot will fall asleep. Use one of the options below.

## Option A — Render paid (simplest reliable Render setup)

* Compute plan `0.5c-512mb` ($7/month) or higher: no spin-down.
* Attach a persistent disk (1 GB, $0.25/month) at `/opt/render/project/src/data`
  and keep `DATA_DIR` at that same path.

`render.yaml` in this repo is already written for this configuration
(`plan: starter`, `DATA_DIR=/opt/render/project/src/data`, disk block commented in
place — uncomment it).

Steps:

1. Push this repository to `main` (see the GitHub section of the final report).
2. Render Dashboard → **New → Blueprint** → select the repo → Apply.
3. In the service's **Environment** tab set: `DISCORD_TOKEN`, `ALLOWED_USER_ID`,
   `GROQ_API_KEY`, `GEMINI_API_KEY` (and any optional integration tokens).
   Leave `GEMINI_MODEL` empty unless you want to pin a model.
4. Deploy. Watch the log for:
   `Health server listening on 0.0.0.0:10000` and `Hermes 3.0 3.0.0 online as ...`
5. Open `https://<service>.onrender.com/health` — expect
   `{"ok": true, "database": true, "workspace_writable": true, ...}`.
6. In Discord: `!health`, `!models`, `!testai`.
   If `GROQ_MODEL` names a retired model, the log shows
   `Groq model selection: configured model '...' is not available; using ...` and the
   bot keeps working — `!model` shows the model actually in use.

## Option B — Render free, kept awake by an external pinger

Keep `plan: free` and `DATA_DIR` inside the project (the default `./data`), then:

1. Set up a free uptime monitor (cron-job.org, UptimeRobot, Better Stack…) to
   `GET https://<service>.onrender.com/health` **every 5–10 minutes**.
   Render counts that inbound request as traffic and the service stays up.
2. Accept the trade-offs: 750 instance hours/month is about 31 days of one service,
   so it fits — but every deploy/restart still wipes the database and workspace.
3. Because a spin-down also loses files, run `!backup` before you deploy anything.

If you would rather not depend on a pinger, use Option C.

## Option C — a small VPS or Railway/Fly (recommended for a real agent)

The agent needs a persistent filesystem more than it needs a managed platform.

**Any Linux VPS (₹350–500/month class is plenty for one user):**

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
git clone https://github.com/Indvishal69/hermes-bot.git /opt/hermes
cd /opt/hermes && python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env        # fill in tokens, keep DATA_DIR=/opt/hermes/data
python agent.py --check && python agent.py
```

Keep it running with systemd:

```ini
# /etc/systemd/system/hermes.service
[Unit]
Description=Hermes 3.0 Discord agent
After=network-online.target

[Service]
Type=simple
User=hermes
WorkingDirectory=/opt/hermes
EnvironmentFile=/opt/hermes/.env
ExecStart=/opt/hermes/.venv/bin/python /opt/hermes/agent.py
Restart=always
RestartSec=5
# Hardening: the agent only needs its own directory
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/hermes/data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now hermes
journalctl -u hermes -f
```

**Railway / Fly.io:** deploy the same `agent.py` with a volume mounted at
`/data` and `DATA_DIR=/data`. Both keep the filesystem across restarts on their
paid tiers; Fly's free allowances change often, so check current pricing.

## Connectors

Connectors read their credentials from environment variables — add the ones you use
(`GITHUB_TOKEN`, `NOTION_TOKEN`, `SLACK_BOT_TOKEN`, `RENDER_API_KEY`, `VERCEL_TOKEN`,
`CLOUDFLARE_API_TOKEN`, `HF_TOKEN`) in the dashboard, then run `!connectors` in Discord.
Enabled/disabled state and saved settings live in the SQLite database, so they survive
restarts but **not** a redeploy on the free plan (ephemeral disk). Details and token
scopes: [CONNECTORS.md](CONNECTORS.md).

## Verifying a deployment

```bash
curl -s https://<host>/health | python -m json.tool
```

A healthy response:

```json
{
  "ok": true,
  "service": "Hermes 3.0",
  "version": "3.0.0",
  "uptime_seconds": 42,
  "database": true,
  "workspace_writable": true,
  "providers_configured": ["gemini", "groq"]
}
```

`ok: false` with `database: false` means `DATA_DIR` is not writable — on Render that
almost always means you pointed it at `/var/data` without a disk. Hermes does not
crash in that case: it logs a warning and falls back to `<project>/data`, so check
the log line `Falling back to DATA_DIR=...` to confirm where data is being written.

## Updating

1. `git pull` (or redeploy the new commit).
2. Migrations run automatically at startup (`schema v1 → v2`); they are additive and
   safe to run on an existing database.
3. If the database matters, `!backup` first and copy the file off the host.

## Rollback

Render keeps the two most recent deploys on paid plans; on free, redeploy an older
commit from the dashboard. The database is not rolled back with the code, and
migrations are forward-only by design (no destructive steps).
