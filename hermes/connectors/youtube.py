"""YouTube connector: channel info, my videos, upload, update.

Uploading needs OAuth2 with a refresh token (the YouTube Data API does not accept
API keys for writes). Create them once with ``scripts/youtube_auth.py``, then set
``YOUTUBE_CLIENT_ID``, ``YOUTUBE_CLIENT_SECRET`` and ``YOUTUBE_REFRESH_TOKEN``.

The upload is a resumable request streamed from a file inside the Hermes
workspace, capped by ``YOUTUBE_MAX_UPLOAD_MB`` (default 128). It is a write tool,
so it always waits for the owner's ``!approve``.
"""

from __future__ import annotations

import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

import requests

from ..tools.files import WorkspaceError
from .base import Connector, ConnectorError, ConnectorTool, json_list, safe_id, safe_path

log = logging.getLogger("hermes.connectors.youtube")

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
PRIVACY = {"private", "unlisted", "public"}


class YouTubeConnector(Connector):
    name = "youtube"
    title = "YouTube"
    description = "Channel stats, your videos, upload a video from the workspace, update details."
    env_vars = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")
    docs_url = "https://developers.google.com/youtube/v3/docs"
    settings_schema = {
        "default_privacy": "private | unlisted | public for uploads (default private)",
    }

    def __init__(self, *args: Any, max_upload_mb: int = 128, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.max_upload_mb = max(1, int(max_upload_mb))
        self._access_token = ""
        self._expires_at = 0.0
        self._http = self.session or requests.Session()

    # -- auth ---------------------------------------------------------------
    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "client_id": self.credential("YOUTUBE_CLIENT_ID"),
                    "client_secret": self.credential("YOUTUBE_CLIENT_SECRET"),
                    "refresh_token": self.credential("YOUTUBE_REFRESH_TOKEN"),
                    "grant_type": "refresh_token",
                },
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise ConnectorError(f"youtube: token refresh failed ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise ConnectorError(
                "youtube: token refresh rejected "
                f"(HTTP {response.status_code}). The refresh token is probably revoked or "
                "the OAuth client changed — re-run scripts/youtube_auth.py."
            )
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise ConnectorError("youtube: token endpoint returned no access_token")
        self._access_token = token
        self._expires_at = time.time() + float(data.get("expires_in", 3_600))
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(
                method,
                f"{API_BASE}{path}",
                headers={**self._headers(), "Content-Type": "application/json"},
                timeout=(10, 45),
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ConnectorError(f"youtube: network error ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise ConnectorError(
                f"youtube: HTTP {response.status_code}: {response.text[:500]}"
            )
        return response.json() if response.content else {}

    # -- read tools ---------------------------------------------------------
    def channel_info(self) -> str:
        data = self._json(
            "GET", "/channels", params={"part": "snippet,statistics,contentDetails", "mine": "true"}
        )
        items = data.get("items") or []
        if not items:
            return "No channel found for this token."
        channel = items[0]
        snippet = channel.get("snippet", {})
        stats = channel.get("statistics", {})
        uploads = (channel.get("contentDetails", {}).get("relatedPlaylists") or {}).get("uploads", "")
        return (
            f"{snippet.get('title')} ({channel.get('id')})\n"
            f"subscribers: {stats.get('subscriberCount', 'hidden')} · "
            f"videos: {stats.get('videoCount', '?')} · views: {stats.get('viewCount', '?')}\n"
            f"uploads playlist: {uploads or '-'}"
        )

    def list_videos(self, limit: str = "10") -> str:
        count = max(1, min(int(limit or 10), 50))
        data = self._json(
            "GET",
            "/search",
            params={
                "part": "snippet",
                "forMine": "true",
                "type": "video",
                "maxResults": count,
                "order": "date",
            },
        )
        rows = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            video_id = (item.get("id") or {}).get("videoId", "")
            rows.append(
                f"{video_id} — {snippet.get('title')} "
                f"({snippet.get('publishedAt', '')[:10]}) "
                f"https://youtu.be/{video_id}"
            )
        return json_list(rows)

    # -- write tools (owner approval required) ------------------------------
    def upload_video(
        self,
        path: str = "",
        title: str = "",
        description: str = "",
        privacy: str = "",
        tags: str = "",
    ) -> str:
        if not (title or "").strip():
            raise ConnectorError("youtube_upload_video needs a title")
        if self.workspace is None:
            raise ConnectorError("youtube: no workspace is attached, cannot read the file")
        try:
            file_path = self.workspace.resolve(safe_path(path), must_exist=True)
        except WorkspaceError as exc:
            raise ConnectorError(f"youtube: {exc}") from exc
        if not file_path.is_file():
            raise ConnectorError(f"youtube: {path} is not a file in the workspace")
        size = file_path.stat().st_size
        if size == 0:
            raise ConnectorError(f"youtube: {path} is empty")
        if size > self.max_upload_mb * 1024 * 1024:
            raise ConnectorError(
                f"youtube: {path} is {size // (1024 * 1024)} MB, over the "
                f"{self.max_upload_mb} MB limit (YOUTUBE_MAX_UPLOAD_MB)"
            )

        privacy_status = (privacy or self.setting("default_privacy", "private")).lower()
        if privacy_status not in PRIVACY:
            raise ConnectorError("privacy must be private, unlisted or public")
        mime = mimetypes.guess_type(file_path.name)[0] or "video/mp4"

        body = {
            "snippet": {
                "title": title[:100],
                "description": (description or "")[:4_900],
                "tags": [tag.strip() for tag in (tags or "").split(",") if tag.strip()][:30],
            },
            "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
        }
        try:
            init = self._http.post(
                f"{UPLOAD_BASE}/videos",
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    **self._headers(),
                    "Content-Type": "application/json",
                    "X-Upload-Content-Length": str(size),
                    "X-Upload-Content-Type": mime,
                },
                json=body,
                timeout=(10, 45),
            )
        except requests.RequestException as exc:
            raise ConnectorError(f"youtube: upload init failed ({type(exc).__name__})") from exc
        if init.status_code >= 400:
            raise ConnectorError(
                f"youtube: upload init rejected (HTTP {init.status_code}): {init.text[:400]}"
            )
        location = init.headers.get("Location")
        if not location:
            raise ConnectorError("youtube: upload session URL missing from the response")

        with file_path.open("rb") as handle:
            try:
                upload = self._http.put(
                    location,
                    headers={"Content-Type": mime, "Content-Length": str(size)},
                    data=handle,
                    timeout=(10, 600),
                )
            except requests.RequestException as exc:
                raise ConnectorError(f"youtube: upload failed ({type(exc).__name__})") from exc
        if upload.status_code >= 400:
            raise ConnectorError(
                f"youtube: upload rejected (HTTP {upload.status_code}): {upload.text[:400]}"
            )
        video = upload.json()
        video_id = video.get("id", "")
        return (
            f"Uploaded '{video.get('snippet', {}).get('title', title)}' as {privacy_status}: "
            f"https://youtu.be/{video_id} ({size // 1024} KB)"
        )

    def update_video(
        self, video_id: str = "", title: str = "", description: str = "", privacy: str = ""
    ) -> str:
        target = safe_id(video_id, what="video id")
        current = self._json("GET", "/videos", params={"part": "snippet,status", "id": target})
        items = current.get("items") or []
        if not items:
            raise ConnectorError(f"youtube: video {target} not found on this channel")
        snippet = dict(items[0].get("snippet") or {})
        status = dict(items[0].get("status") or {})
        if title:
            snippet["title"] = title[:100]
        if description:
            snippet["description"] = description[:4_900]
        if privacy:
            if privacy.lower() not in PRIVACY:
                raise ConnectorError("privacy must be private, unlisted or public")
            status["privacyStatus"] = privacy.lower()
        self._json(
            "PUT",
            "/videos",
            params={"part": "snippet,status"},
            json={"id": target, "snippet": snippet, "status": status},
        )
        return f"Updated video {target}: https://youtu.be/{target}"

    # -- registry -----------------------------------------------------------
    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool("youtube_channel_info", "Channel title, subscribers and video count.",
                          {}, self.channel_info),
            ConnectorTool("youtube_list_videos", "List your recent uploads.",
                          {"limit": "int (optional, default 10)"}, self.list_videos),
            ConnectorTool("youtube_upload_video",
                          "Upload a video file from the workspace to YouTube.",
                          {"path": "str (required, workspace-relative video file)",
                           "title": "str (required)", "description": "str (optional)",
                           "privacy": "private|unlisted|public (optional)",
                           "tags": "comma separated (optional)"},
                          self.upload_video, write=True),
            ConnectorTool("youtube_update_video", "Change a video's title, description or privacy.",
                          {"video_id": "str (required)", "title": "str (optional)",
                           "description": "str (optional)", "privacy": "str (optional)"},
                          self.update_video, write=True),
        ]

    def check(self) -> str:
        return self.channel_info()
