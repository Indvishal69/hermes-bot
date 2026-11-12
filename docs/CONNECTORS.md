# Connectors

A connector is how Hermes talks to an outside service — GitHub, Notion, Slack, Render,
Vercel, Cloudflare, Hugging Face. The agent gets a small set of named tools, you keep
control of what it may change, and the connection survives restarts.

## How it works

```
environment variables            SQLite                      agent
(DISCORD_TOKEN, GITHUB_TOKEN…)   connectors table            ┌──────────────┐
        │                        (enabled flag, settings)    │ system prompt│
        ▼                              │                     │  + tools     │
   ConnectorRegistry ──────────────────┴────────────────────▶└──────┬───────┘
   (builds connectors, checks env,                                │ one JSON action
    exposes only active tools)                                    ▼
                                                          read tool → runs
                                                          write tool → !approve
```

Three rules the code enforces:

1. **Credentials live only in environment variables.** They are never written to the
   database, never echoed in Discord, and are redacted from every error message.
2. **State lives in SQLite.** `!disconnect notion` is remembered after a restart;
   settings like `default_repo` persist too. (On a Render free instance the database
   itself is wiped on redeploy — see [DEPLOYMENT.md](DEPLOYMENT.md).)
3. **The model cannot pick a host.** Each connector builds its own URLs from a fixed
   base; arguments are validated as identifiers (`owner/name`, a zone id, a path with
   no `..`). A tool call cannot reach an arbitrary endpoint.

## Commands

| Command | What it does |
| --- | --- |
| `!connectors` | List every connector: connected / configured but disconnected / missing env vars |
| `!connect github` | Enable a connector (needs its env var set first) — saved |
| `!disconnect github` | Disable it; its tools disappear from the agent immediately — saved |
| `!connector github` | Detail: env var names, saved settings, tools, which ones need approval |
| `!connector github test` | Live check (`whoami`, token verify, `auth.test`, …) |
| `!connector github set default_repo=Indvishal69/hermes-bot` | Save a non-secret setting |
| `!connector github set default_repo=` | Clear it |

`!integrations` is kept as an alias for `!connectors`.

## Available connectors

| Connector | Environment variable | Tools (R = read, W = needs `!approve`) |
| --- | --- | --- |
| `github` | `GITHUB_TOKEN` | `github_whoami` R, `github_list_repos` R, `github_get_repo` R, `github_list_branches` R, `github_get_file` R, `github_list_issues` R, `github_list_pulls` R, `github_create_issue` W, `github_commit_file` W |
| `notion` | `NOTION_TOKEN` | `notion_search` R, `notion_get_page` R, `notion_query_database` R, `notion_append_text` W |
| `slack` | `SLACK_BOT_TOKEN` | `slack_list_channels` R, `slack_post_message` W |
| `netlify` | `NETLIFY_TOKEN` | `netlify_list_sites` R, `netlify_get_site` R, `netlify_list_deploys` R, `netlify_get_deploy` R, `netlify_trigger_build` W, `netlify_cancel_deploy` W |
| `render` | `RENDER_API_KEY` | `render_list_services` R, `render_list_deploys` R, `render_trigger_deploy` W |
| `vercel` | `VERCEL_TOKEN` | `vercel_list_projects` R, `vercel_list_deployments` R |
| `cloudflare` | `CLOUDFLARE_API_TOKEN` | `cloudflare_verify_token` R, `cloudflare_purge_cache` W |
| `huggingface` | `HF_TOKEN` | `hf_whoami` R, `hf_search_models` R |

A connector is **active** when its env var is set *and* you have not disconnected it.
Missing env vars are shown by name in `!connectors` — never their values.

### Token scopes that work

* **GitHub** — fine-grained token with *Contents: read/write* and *Issues: read/write*
  on the repositories you want; or a classic token with `repo`. Read-only tools work
  with `Contents: read`.
* **Notion** — create an internal integration, share the pages/databases with it, copy
  the token. A pasted Notion URL is accepted wherever an id is asked for.
* **Slack** — bot token with `channels:read` and `chat:write`.
* **Netlify** — personal access token (User settings → Applications → Personal access
  tokens). Read tools work with any token; `netlify_trigger_build` needs the *build*
  permission, and the site must have build settings or a linked repo.
* **Render** — API key from Account Settings → API Keys.
* **Vercel** — token from Account Settings → Tokens.
* **Cloudflare** — API token with *Zone.Cache Purge* for the zone you name.
* **Hugging Face** — a read token is enough.

## Saved settings

| Connector | Setting | Meaning |
| --- | --- | --- |
| `github` | `default_repo` | `owner/name` used when a tool call omits `repo` |
| `notion` | `default_page`, `default_database` | used when an id is omitted |
| `slack` | `default_channel` | channel id or `#name` |
| `netlify` | `default_site` | site id or site name |
| `render` | `default_service` | service id |
| `cloudflare` | `default_zone` | zone id |

Settings are stored in the `connectors` table, not in the prompt, so they cost no
tokens and cannot be edited by the model.

## Write actions and approvals

A write tool never runs on the model's word alone. The flow is the same as for shell
commands:

1. The model returns `{"action":"github_create_issue","title":"…","repo":"…","reason":"…"}`.
2. Hermes sends the request with an ID and waits.
3. You type `!approve <id>` — the call runs, the result is shown and the agent
   continues. `!deny <id>` returns "nothing ran" so the model adapts instead of
   pretending it succeeded.
4. Both the request and the outcome go to the audit log (`!status` shows the queue).

## Adding your own connector

Create `hermes/connectors/mine.py`:

```python
from .base import Connector, ConnectorError, ConnectorTool

class MineConnector(Connector):
    name = "mine"
    title = "Mine"
    description = "What this service is for."
    env_vars = ("MINE_TOKEN",)                 # names only, never values
    settings_schema = {"default_project": "project id used when omitted"}

    def _client(self):
        return self.client(
            "https://api.mine.example/v1",
            {"Authorization": f"Bearer {self.credential('MINE_TOKEN')}"},
        )

    def list_things(self) -> str:
        data = self._client().request("GET", "/things", params={"limit": 20})
        return "\n".join(item["name"] for item in data["items"])

    def create_thing(self, name: str = "") -> str:
        if not name:
            raise ConnectorError("create_thing needs a name")
        data = self._client().request("POST", "/things", json_body={"name": name})
        return f"created {data['id']}"

    def tools(self):
        return [
            ConnectorTool("mine_list_things", "List things.", {}, self.list_things),
            ConnectorTool("mine_create_thing", "Create a thing.",
                          {"name": "str (required)"}, self.create_thing, write=True),
        ]

    def check(self) -> str:
        self._client().request("GET", "/ping")
        return "token works"
```

Then add `MineConnector` to `CONNECTOR_CLASSES` in `hermes/connectors/registry.py` and
to `hermes/connectors/__init__.py`. It appears in `!connectors` immediately; set
`MINE_TOKEN` on the host and run `!connector mine test`.

Rules to keep: never build a URL from model input, mark anything that changes remote
state as `write=True`, raise `ConnectorError` (not bare exceptions) for expected
failures, and never include a token in a returned string.

## Troubleshooting

| Symptom | Meaning |
| --- | --- |
| `not configured — needs GITHUB_TOKEN` | env var missing on the host; set it in Render → Environment and redeploy |
| `configured but disconnected` | you ran `!disconnect`; `!connect <name>` brings it back |
| `HTTP 401/403 … check that the token is valid and has the required scope` | token wrong, expired, or missing a scope |
| `HTTP 404 … check the id/path` | wrong repo/path/page id |
| `no default_repo given and no default saved` | pass `repo` in the call or run `!connector github set default_repo=owner/name` |
| `Connector tool 'x' is not available` | that connector is disconnected or unconfigured |
| Settings disappeared after a deploy | `DATA_DIR` is on an ephemeral disk — see [DEPLOYMENT.md](DEPLOYMENT.md) |
