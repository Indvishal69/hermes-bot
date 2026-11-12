"""Web access for Hermes: fetch pages, search, and optionally drive a browser.

Two layers, because they have very different costs:

* **Fetch** (always on) — plain HTTP with a strict SSRF guard, a size cap and a
  stdlib HTML→text extractor. No JavaScript, a few MB of RAM.
* **Browser** (opt-in, ``BROWSER_ENABLED=true``) — a real headless Chromium through
  Playwright for JavaScript-rendered pages. It needs ~300–500 MB RAM, which does not
  fit a Render free instance (512 MB total, shared with Discord and Flask).

Everything the model reads back is marked as untrusted data: a web page can contain
text that looks like instructions, and Hermes must treat it as content, never as a
command.
"""

from .browser import BrowserSession, browser_available
from .fetch import FetchResult, HttpFetcher, extract_text, extract_title, extract_links
from .search import SearchClient, search_configured
from .tools import WebTools

__all__ = [
    "BrowserSession",
    "browser_available",
    "FetchResult",
    "HttpFetcher",
    "extract_text",
    "extract_title",
    "extract_links",
    "SearchClient",
    "search_configured",
    "WebTools",
]
