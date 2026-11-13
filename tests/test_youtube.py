"""YouTube connector: token refresh, reads, resumable upload, safety limits."""

from __future__ import annotations

import json

import pytest

from hermes.connectors.base import ConnectorError
from hermes.connectors.youtube import YouTubeConnector
from hermes.tools import Workspace

CREDS = {
    "YOUTUBE_CLIENT_ID": "client-id",
    "YOUTUBE_CLIENT_SECRET": "client-secret",
    "YOUTUBE_REFRESH_TOKEN": "refresh-token",
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = self._payload if isinstance(self._payload, str) else json.dumps(self._payload)
        self.content = self.text.encode()

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeHttp:
    """Routes by URL fragment so each test reads as a small story."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls: list[dict] = []

    def _route(self, method, url, **kwargs):
        entry = {"method": method, "url": url, **{k: v for k, v in kwargs.items() if k != "data"}}
        entry["form"] = kwargs.get("data")
        self.calls.append(entry)
        from urllib.parse import urlencode

        haystack = " ".join(
            [
                method,
                url,
                urlencode(kwargs.get("params") or {}),
                json.dumps(kwargs.get("json") or {})[:400],
            ]
        )
        for index, (fragment, response) in enumerate(self.routes):
            if fragment in haystack:
                self.routes.pop(index)
                return response() if callable(response) else response
        return FakeResponse(200, {})

    def post(self, url, **kwargs):
        return self._route("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._route("PUT", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._route(method, url, **kwargs)


TOKEN_OK = ("oauth2.googleapis.com/token", FakeResponse(200, {"access_token": "ya29.token", "expires_in": 3600}))


def make_connector(routes, workspace=None, **kwargs):
    http = FakeHttp(routes)
    connector = YouTubeConnector(CREDS, kwargs.pop("settings", {}), session=http,
                                workspace=workspace, **kwargs)
    return connector, http


def test_token_is_fetched_once_and_cached():
    connector, http = make_connector([
        TOKEN_OK,
        ("/channels", FakeResponse(200, {"items": [{"id": "UC1", "snippet": {"title": "My Channel"},
                                                   "statistics": {"subscriberCount": "12",
                                                                  "videoCount": "3",
                                                                  "viewCount": "400"},
                                                   "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})),
        ("/channels", FakeResponse(200, {"items": [{"id": "UC1", "snippet": {"title": "My Channel"},
                                                   "statistics": {}, "contentDetails": {}}]})),
    ])
    first = connector.channel_info()
    assert "My Channel" in first and "subscribers: 12" in first
    connector.channel_info()
    token_calls = [c for c in http.calls if "oauth2.googleapis.com" in c["url"]]
    assert len(token_calls) == 1


def test_channel_info_with_no_channel():
    connector, _ = make_connector([TOKEN_OK, ("/channels", FakeResponse(200, {"items": []}))])
    assert "No channel" in connector.channel_info()


def test_token_refresh_failure_is_explained():
    connector, _ = make_connector([
        ("oauth2.googleapis.com/token", FakeResponse(400, {"error": "invalid_grant"})),
    ])
    with pytest.raises(ConnectorError) as excinfo:
        connector.channel_info()
    assert "youtube_auth.py" in str(excinfo.value)


def test_list_videos_formats_links():
    connector, _ = make_connector([
        TOKEN_OK,
        ("/search", FakeResponse(200, {"items": [
            {"id": {"videoId": "abc123"}, "snippet": {"title": "Snake game", "publishedAt": "2026-11-01T10:00:00Z"}}
        ]})),
    ])
    text = connector.list_videos()
    assert "abc123" in text and "https://youtu.be/abc123" in text


def test_upload_streams_the_file_and_returns_the_link(workspace):
    workspace.write_text("videos/demo.mp4", "fake video bytes")
    connector, http = make_connector(
        [
            TOKEN_OK,
            ("POST https://www.googleapis.com/upload/youtube/v3/videos",
             FakeResponse(200, {}, {"Location": "https://upload.example/session-1"})),
            ("PUT https://upload.example/session-1",
             FakeResponse(200, {"id": "vid42", "snippet": {"title": "Demo"}})),
        ],
        workspace=workspace,
    )
    message = connector.upload_video("videos/demo.mp4", title="Demo", privacy="unlisted", tags="a, b")
    assert "https://youtu.be/vid42" in message
    assert "unlisted" in message

    init = next(c for c in http.calls if c["method"] == "POST" and "upload/youtube/v3/videos" in c["url"])
    assert init["params"]["uploadType"] == "resumable"
    assert init["headers"]["X-Upload-Content-Length"] == str(len("fake video bytes"))
    assert init["json"]["status"]["privacyStatus"] == "unlisted"
    assert init["json"]["snippet"]["tags"] == ["a", "b"]

    put = next(c for c in http.calls if c["method"] == "PUT")
    assert put["url"] == "https://upload.example/session-1"


def test_upload_requires_a_title_and_a_real_file(workspace):
    connector, _ = make_connector([TOKEN_OK], workspace=workspace)
    with pytest.raises(ConnectorError):
        connector.upload_video("missing.mp4", title="x")

    workspace.write_text("videos/demo.mp4", "bytes")
    with pytest.raises(ConnectorError) as excinfo:
        connector.upload_video("videos/demo.mp4", title="")
    assert "title" in str(excinfo.value)


def test_upload_refuses_paths_outside_the_workspace(workspace):
    connector, _ = make_connector([TOKEN_OK], workspace=workspace)
    with pytest.raises(ConnectorError):
        connector.upload_video("../../etc/passwd", title="x")


def test_upload_respects_the_size_cap(workspace):
    workspace.write_text("videos/big.mp4", "x" * 5_000)
    connector, _ = make_connector([TOKEN_OK], workspace=workspace, max_upload_mb=1)
    connector.max_upload_mb = 0.001  # ~1 KB, so the 5 KB file is too big
    with pytest.raises(ConnectorError) as excinfo:
        connector.upload_video("videos/big.mp4", title="x")
    assert "limit" in str(excinfo.value)


def test_upload_rejects_a_bad_privacy_value(workspace):
    workspace.write_text("videos/demo.mp4", "bytes")
    connector, _ = make_connector([TOKEN_OK], workspace=workspace)
    with pytest.raises(ConnectorError) as excinfo:
        connector.upload_video("videos/demo.mp4", title="x", privacy="everyone")
    assert "privacy" in str(excinfo.value)


def test_update_video_merges_existing_fields():
    connector, http = make_connector([
        TOKEN_OK,
        ("GET https://www.googleapis.com/youtube/v3/videos",
         FakeResponse(200, {"items": [{"snippet": {"title": "old", "description": "d"},
                                      "status": {"privacyStatus": "private"}}]})),
        ("PUT https://www.googleapis.com/youtube/v3/videos", FakeResponse(200, {"id": "vid42"})),
    ])
    assert "vid42" in connector.update_video("vid42", title="new title")
    put = [c for c in http.calls if c["method"] == "PUT"][0]
    assert put["url"].startswith("https://www.googleapis.com/youtube/v3/videos")


def test_write_tools_are_marked_for_approval():
    connector, _ = make_connector([])
    writes = {tool.name for tool in connector.tools() if tool.write}
    assert writes == {"youtube_upload_video", "youtube_update_video"}
