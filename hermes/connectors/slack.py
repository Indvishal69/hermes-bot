"""Slack connector: list channels, post a message (approval required)."""

from __future__ import annotations

from .base import Connector, ConnectorError, ConnectorTool, json_list


class SlackConnector(Connector):
    name = "slack"
    title = "Slack"
    description = "Read channels and post messages as the bot."
    env_vars = ("SLACK_BOT_TOKEN",)
    docs_url = "https://api.slack.com/methods"
    settings_schema = {"default_channel": "channel id or #name used when a tool call omits 'channel'"}
    base = "https://slack.com/api"

    def _client(self):
        return self.client(
            self.base,
            {
                "Authorization": f"Bearer {self.credential('SLACK_BOT_TOKEN')}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )

    def _call(self, method: str, body: dict | None = None) -> dict:
        data = self._client().request("POST", f"/{method}", json_body=body or {})
        if isinstance(data, dict) and data.get("ok") is False:
            raise ConnectorError(f"slack {method} failed: {data.get('error', 'unknown_error')}")
        return data if isinstance(data, dict) else {}

    def list_channels(self, limit: str = "20") -> str:
        count = max(1, min(int(limit or 20), 100))
        data = self._call("conversations.list", {"limit": count, "exclude_archived": True})
        rows = [
            f"#{item.get('name')} ({item.get('id')}) members={item.get('num_members', '?')}"
            for item in data.get("channels", [])
        ]
        return json_list(rows)

    def post_message(self, text: str = "", channel: str = "") -> str:
        if not (text or "").strip():
            raise ConnectorError("slack_post_message needs text")
        target = (channel or self.require_setting("default_channel", "channel")).strip()
        if target.startswith("#"):
            target = target[1:]
        data = self._call("chat.postMessage", {"channel": target, "text": text[:3900]})
        return f"Posted to {data.get('channel', target)} (ts={data.get('ts')})"

    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool("slack_list_channels", "List channels the bot can see.",
                          {"limit": "int (optional, default 20)"}, self.list_channels),
            ConnectorTool("slack_post_message", "Post a message to a channel.",
                          {"text": "str (required)",
                           "channel": "str (optional if default_channel saved)"},
                          self.post_message, write=True),
        ]

    def check(self) -> str:
        data = self._call("auth.test")
        return f"Slack bot {data.get('user')} on team {data.get('team')} ({data.get('url')})"
