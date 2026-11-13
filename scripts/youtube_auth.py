#!/usr/bin/env python3
"""One-time helper: get a YouTube refresh token for Hermes.

The YouTube Data API refuses API keys for uploads — it needs OAuth2 with a refresh
token. Run this locally, once:

    python scripts/youtube_auth.py

Steps it walks you through:
  1. Google Cloud project → enable "YouTube Data API v3".
  2. OAuth consent screen → add your Google account as a test user.
  3. Credentials → OAuth client ID → type "Desktop app" → copy the client id/secret.
  4. Run this script with those values.

It prints a refresh token. Put all three in the host's environment
(YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN) and redeploy.
The token is shown once here — treat it like a password.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

try:
    import requests
except ImportError:  # pragma: no cover - dependency guidance
    print("This script needs requests:  pip install requests", file=sys.stderr)
    raise SystemExit(1)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube",
]
# "Desktop app" clients accept any localhost port; we never actually listen.
REDIRECT_URI = "http://localhost:8080/"


def prompt(label: str, env_name: str, secret: bool = False) -> str:
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    if secret:
        import getpass

        return getpass.getpass(f"{label}: ").strip()
    return input(f"{label}: ").strip()


def main() -> int:
    print(__doc__)
    client_id = prompt("OAuth client id", "YOUTUBE_CLIENT_ID")
    client_secret = prompt("OAuth client secret", "YOUTUBE_CLIENT_SECRET", secret=True)
    if not client_id or not client_secret:
        print("Both client id and secret are required.", file=sys.stderr)
        return 1

    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",          # force a refresh token even on re-auth
            "include_granted_scopes": "true",
        }
    )
    print("\n1. Open this URL in the browser where you are signed in to YouTube:\n")
    print(f"{AUTH_URL}?{query}\n")
    print("2. Approve access. The browser will fail to load http://localhost:8080/…")
    print("   That is expected — copy the FULL address bar URL and paste it below.\n")

    pasted = input("Redirected URL (or just the code): ").strip()
    if not pasted:
        print("Nothing pasted.", file=sys.stderr)
        return 1
    if "code=" in pasted:
        code = parse_qs(urlsplit(pasted).query).get("code", [""])[0]
    else:
        code = pasted
    if not code:
        print("Could not find a 'code' parameter in that URL.", file=sys.stderr)
        return 1

    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        print(f"\nToken exchange failed (HTTP {response.status_code}):", file=sys.stderr)
        print(response.text, file=sys.stderr)
        return 1

    payload = response.json()
    refresh = payload.get("refresh_token", "")
    if not refresh:
        print(
            "\nNo refresh_token came back. Remove Hermes from "
            "https://myaccount.google.com/permissions and run this again.",
            file=sys.stderr,
        )
        return 1

    print("\nSuccess. Add these three variables to your host and redeploy:\n")
    print(f"YOUTUBE_CLIENT_ID={client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={client_secret}")
    print(f"YOUTUBE_REFRESH_TOKEN={refresh}")
    print("\nThen in Discord: !connectors → !connector youtube test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
