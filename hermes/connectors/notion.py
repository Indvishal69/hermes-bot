"""Notion connector: search, read pages, query a database, append text."""

from __future__ import annotations

import re

from .base import Connector, ConnectorError, ConnectorTool, json_list, safe_id


class NotionConnector(Connector):
    name = "notion"
    title = "Notion"
    description = "Search the workspace, read pages, query a database, append notes."
    env_vars = ("NOTION_TOKEN",)
    docs_url = "https://developers.notion.com/reference/intro"
    settings_schema = {
        "default_database": "database id used when a tool call omits 'database_id'",
        "default_page": "page id used when a tool call omits 'page_id'",
    }
    base = "https://api.notion.com/v1"
    api_version = "2022-06-28"

    def _client(self):
        return self.client(
            self.base,
            {
                "Authorization": f"Bearer {self.credential('NOTION_TOKEN')}",
                "Notion-Version": self.api_version,
                "Content-Type": "application/json",
            },
        )

    @staticmethod
    def _plain_text(rich: list[dict] | None) -> str:
        parts = []
        for item in rich or []:
            text = item.get("plain_text")
            if text:
                parts.append(text)
        return "".join(parts).strip()

    @staticmethod
    def _extract_id(raw: str, *, what: str) -> str:
        """Accept a bare id, a dashed UUID, or a pasted Notion URL."""
        text = (raw or "").strip()
        if not text:
            raise ConnectorError(f"no {what} given")
        # A Notion id is 32 hex characters at the end of a pasted URL, with or
        # without dashes. Anything else must already be a plain identifier.
        text = text.split("?")[0].split("#")[0].rstrip("/")
        compact = text.replace("-", "")
        match = re.search(r"([0-9a-fA-F]{32})$", compact)
        if match:
            return match.group(1).lower()
        return safe_id(compact, what=what)

    def _page_id(self, page_id: str = "") -> str:
        return self._extract_id(
            page_id or self.require_setting("default_page", "page id"), what="page id"
        )

    def _database_id(self, database_id: str = "") -> str:
        return self._extract_id(
            database_id or self.require_setting("default_database", "database id"),
            what="database id",
        )

    # -- read tools ---------------------------------------------------------
    def search(self, query: str = "") -> str:
        body: dict = {"page_size": 20}
        if (query or "").strip():
            body["query"] = query.strip()[:200]
        data = self._client().request("POST", "/search", json_body=body)
        rows = []
        for item in data.get("results", [])[:20]:
            title = ""
            props = item.get("properties") or {}
            for value in props.values():
                if value.get("type") == "title":
                    title = self._plain_text(value.get("title"))
                    break
            if not title:
                title = self._plain_text((item.get("title") or []))
            rows.append(f"{item.get('object')} {item.get('id')} — {title or '(untitled)'}")
        return json_list(rows)

    def get_page(self, page_id: str = "") -> str:
        client = self._client()
        target = self._page_id(page_id)
        page = client.request("GET", f"/pages/{target}")
        blocks = client.request("GET", f"/blocks/{target}/children", params={"page_size": 100})
        lines = []
        for block in blocks.get("results", []):
            text = self._plain_text(block.get("paragraph", {}).get("rich_text"))
            if text:
                lines.append(text)
        title = ""
        for value in (page.get("properties") or {}).values():
            if value.get("type") == "title":
                title = self._plain_text(value.get("title"))
                break
        body = "\n".join(lines) or "(page has no paragraph text)"
        return f"Page {target} — {title or '(untitled)'}\n{body[: self.max_chars]}"

    def query_database(self, database_id: str = "") -> str:
        target = self._database_id(database_id)
        data = self._client().request(
            "POST", f"/databases/{target}/query", json_body={"page_size": 25}
        )
        rows = []
        for item in data.get("results", []):
            cells = []
            for name, value in (item.get("properties") or {}).items():
                kind = value.get("type")
                if kind == "title":
                    cells.append(self._plain_text(value.get("title")))
                elif kind == "rich_text":
                    cells.append(self._plain_text(value.get("rich_text")))
                elif kind == "select" and value.get("select"):
                    cells.append(str(value["select"].get("name")))
                elif kind == "number":
                    cells.append(str(value.get("number")))
            rows.append(f"{item.get('id')} — " + " | ".join(cell for cell in cells if cell))
        return json_list(rows)

    # -- write tool (owner approval required) -------------------------------
    def append_text(self, text: str = "", page_id: str = "") -> str:
        if not (text or "").strip():
            raise ConnectorError("notion_append_text needs some text")
        target = self._page_id(page_id)
        body = {
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]},
                }
            ]
        }
        data = self._client().request("POST", f"/blocks/{target}/children", json_body=body)
        return f"Appended {len(data.get('results', []))} block(s) to page {target}."

    # -- registry -----------------------------------------------------------
    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool("notion_search", "Search pages and databases in the workspace.",
                          {"query": "str (optional; empty lists recent items)"}, self.search),
            ConnectorTool("notion_get_page", "Read a page's text.",
                          {"page_id": "str (optional if default_page saved)"}, self.get_page),
            ConnectorTool("notion_query_database", "List rows of a database.",
                          {"database_id": "str (optional if default_database saved)"},
                          self.query_database),
            ConnectorTool("notion_append_text", "Append a paragraph to a page.",
                          {"text": "str (required)", "page_id": "str (optional)"},
                          self.append_text, write=True),
        ]

    def check(self) -> str:
        data = self._client().request("POST", "/search", json_body={"page_size": 1})
        return f"Workspace reachable ({len(data.get('results', []))} item(s) visible to the integration)."
