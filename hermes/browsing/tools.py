"""The tool surface the agent sees for web access."""

from __future__ import annotations

import logging
from typing import Any

from ..utils import truncate
from .browser import BrowserSession, browser_available
from .fetch import FetchError, HttpFetcher
from .search import SearchClient, SearchError

log = logging.getLogger("hermes.browsing.tools")


class WebToolError(RuntimeError):
    pass


class WebTools:
    """Bundles the fetcher, the optional browser and search into agent tools."""

    def __init__(self, settings, *, session=None):
        self.settings = settings
        self.enabled = bool(settings.web_enabled)
        self.fetcher = HttpFetcher(
            timeout=settings.web_timeout,
            max_bytes=settings.web_max_bytes,
            max_chars=settings.web_max_chars,
            session=session,
            allowed_domains=settings.web_allowed_domains,
        )
        self.searcher = SearchClient(
            provider=settings.search_provider,
            api_key=settings.search_api_key,
            searxng_url=settings.searxng_url,
            timeout=settings.web_timeout,
            session=session,
            secrets=settings.secrets,
        )
        self.browser = (
            BrowserSession(
                timeout=settings.browser_timeout,
                max_chars=settings.web_max_chars,
            )
            if settings.browser_enabled
            else None
        )

    # -- capability reporting ----------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "search": self.searcher.status(),
            "browser_enabled": bool(self.browser),
            "browser_installed": browser_available(),
        }

    def specs(self) -> dict[str, dict[str, Any]]:
        """Only offer tools that can actually work right now."""
        if not self.enabled:
            return {}
        specs: dict[str, dict[str, Any]] = {
            "web_fetch": {
                "args": {"url": "str (required, http/https)",
                         "max_chars": "int (optional, default from config)"},
                "desc": "Download a public web page and return its readable text "
                        "(HTML/JSON/XML, no JavaScript).",
                "approval": False,
            },
            "web_links": {
                "args": {"url": "str (required)"},
                "desc": "List the links on a public web page.",
                "approval": False,
            },
        }
        if self.searcher.configured:
            specs["web_search"] = {
                "args": {"query": "str (required)", "limit": "int (optional, default 5)"},
                "desc": f"Search the web ({self.searcher.provider}) and return titles, URLs and snippets.",
                "approval": False,
            }
        if self.browser is not None:
            specs["browser_open"] = {
                "args": {"url": "str (required)",
                         "wait": "domcontentloaded|load|networkidle (optional)"},
                "desc": "Open a page in a real headless browser so JavaScript runs, "
                        "then return the visible text.",
                "approval": False,
            }
        return specs

    # -- execution ----------------------------------------------------------
    def execute(self, name: str, args: dict[str, Any]) -> str:
        if name not in self.specs():
            raise WebToolError(
                f"'{name}' is not available. Web access is "
                f"{'enabled' if self.enabled else 'disabled (WEB_ENABLED=false)'}"
                + ("" if self.browser is None else ", browser is on")
                + ("" if self.searcher.configured else ", web search is not configured")
                + "."
            )
        try:
            if name == "web_fetch":
                return self._fetch(args)
            if name == "web_links":
                return self.fetcher.links(str(args.get("url", "")))
            if name == "web_search":
                return self.searcher.search(
                    str(args.get("query", "")), int(args.get("limit", 5) or 5)
                )
            if name == "browser_open":
                assert self.browser is not None
                return self.browser.open(str(args.get("url", "")), str(args.get("wait", "")))
        except (FetchError, SearchError) as exc:
            raise WebToolError(str(exc)) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise WebToolError(f"{name} failed: {type(exc).__name__}: {exc}") from exc
        raise WebToolError(f"Unhandled web tool '{name}'.")

    def _fetch(self, args: dict[str, Any]) -> str:
        result = self.fetcher.fetch(str(args.get("url", "")))
        limit = int(args.get("max_chars", 0) or 0)
        if limit <= 0 or limit > self.settings.web_max_chars:
            limit = self.settings.web_max_chars
        return result.as_tool_text(max_chars=limit)

    # -- helpers ------------------------------------------------------------
    def preview(self, url: str) -> str:
        """Used by ``!web`` to show what the agent would see."""
        result = self.fetcher.fetch(url)
        head = truncate(result.text, 500, marker="…")
        return (
            f"{result.final_url}\nHTTP {result.status} · {result.bytes_read} bytes\n"
            f"title: {result.title or '(none)'}\n---\n{head}"
        )
