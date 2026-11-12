"""Connector framework: safety, persistence, tool exposure, real API shapes."""

from __future__ import annotations

import json

import pytest

from hermes.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorTool,
    HttpClient,
    safe_id,
    safe_path,
    safe_repo,
)
from hermes.connectors.github import GitHubConnector
from hermes.connectors.netlify import NetlifyConnector
from hermes.connectors.notion import NotionConnector
from hermes.connectors.platforms import CloudflareConnector, RenderConnector
from hermes.connectors.registry import ConnectorRegistry
from hermes.connectors.slack import SlackConnector
from hermes.db import Database


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, params=None, json=None, data=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "params": params, "json": json,
             "data": data, "headers": headers}
        )
        if not self.responses:
            return FakeResponse(200, {})
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(*outcome)


class EchoConnector(Connector):
    """Deterministic connector used to test the registry itself."""

    name = "echo"
    title = "Echo"
    description = "Test connector"
    env_vars = ("ECHO_TOKEN",)
    settings_schema = {"default_target": "target used when omitted"}

    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool("echo_ping", "Ping", {}, self.ping),
            ConnectorTool("echo_target", "Show default target", {}, self.show_target),
            ConnectorTool("echo_write", "Write something",
                          {"text": "str (required)"}, self.write, write=True),
        ]

    def ping(self) -> str:
        return "pong"

    def show_target(self) -> str:
        return self.require_setting("default_target", "target")

    def write(self, text: str = "") -> str:
        if not text:
            raise ConnectorError("echo_write needs text")
        return f"wrote {text}"

    def check(self) -> str:
        return "echo ok"


def make_registry(db, *, env=None, secrets=()):
    environment = {"ECHO_TOKEN": "echo-secret-token-123456"}
    environment.update(env or {})
    return ConnectorRegistry(
        db, env=environment, secrets=secrets, classes=(EchoConnector,)
    )


# --------------------------------------------------------------------------
# input validation
# --------------------------------------------------------------------------
def test_safe_identifiers_accept_normal_values():
    assert safe_id("abc123") == "abc123"
    assert safe_id("zone-1_2.3") == "zone-1_2.3"
    assert safe_path("docs/readme.md") == "docs/readme.md"
    assert safe_path("/docs/readme.md") == "docs/readme.md"
    assert safe_repo("Indvishal69/hermes-bot") == "Indvishal69/hermes-bot"


@pytest.mark.parametrize(
    "value",
    ["", "a b", "../etc", "a/b", "a?x=1", "http://evil", "a\x00b", "-"],
)
def test_safe_id_rejects_hostile_values(value):
    with pytest.raises(ConnectorError):
        safe_id(value)


@pytest.mark.parametrize("value", ["", "../x", "a/../../b", "a b", "a?b"])
def test_safe_path_rejects_traversal(value):
    with pytest.raises(ConnectorError):
        safe_path(value)


@pytest.mark.parametrize("value", ["no-slash", "a/b/c", "a//b", "a/b c"])
def test_safe_repo_requires_owner_name(value):
    with pytest.raises(ConnectorError):
        safe_repo(value)


def test_http_client_refuses_absolute_urls_and_traversal():
    client = HttpClient("test", "https://api.example.com")
    with pytest.raises(ConnectorError):
        client.request("GET", "https://evil.example.com/x")
    with pytest.raises(ConnectorError):
        client.request("GET", "/a/../b")


def test_http_client_adds_a_hint_for_auth_failures():
    client = HttpClient("test", "https://api.example.com",
                        session=FakeSession([(401, {"message": "Bad credentials"})]))
    with pytest.raises(ConnectorError) as excinfo:
        client.request("GET", "/user")
    assert "401" in str(excinfo.value)
    assert "token" in str(excinfo.value)


def test_tool_call_rejects_unknown_arguments():
    tool = ConnectorTool("x", "desc", {"a": "str"}, lambda a="": a)
    assert tool.call({"a": "v"}) == "v"
    with pytest.raises(ConnectorError):
        tool.call({"nope": 1})


# --------------------------------------------------------------------------
# registry behaviour
# --------------------------------------------------------------------------
def test_configured_connector_is_active_by_default(settings):
    db = Database(settings.db_path); db.init()
    registry = make_registry(db)
    assert registry.is_active("echo") is True
    assert set(registry.active_tools()) == {"echo_ping", "echo_target", "echo_write"}


def test_unconfigured_connector_is_inactive_and_lists_missing_env(settings):
    db = Database(settings.db_path); db.init()
    registry = make_registry(db, env={"ECHO_TOKEN": ""})
    assert registry.is_active("echo") is False
    assert registry.active_tools() == {}
    status = registry.status()[0]
    assert status["configured"] is False
    assert status["missing_env"] == ["ECHO_TOKEN"]


def test_disable_hides_tools_and_persists(settings):
    db = Database(settings.db_path); db.init()
    registry = make_registry(db)
    registry.disable("echo")
    assert registry.is_active("echo") is False
    assert registry.active_tools() == {}

    # A brand-new registry (i.e. a restart) must remember the choice.
    restarted = make_registry(db)
    assert restarted.is_active("echo") is False
    assert restarted.is_disabled("echo") is True

    restarted.enable("echo")
    assert make_registry(db).is_active("echo") is True


def test_enable_requires_credentials(settings):
    db = Database(settings.db_path); db.init()
    registry = make_registry(db, env={"ECHO_TOKEN": ""})
    with pytest.raises(ConnectorError) as excinfo:
        registry.enable("echo")
    assert "ECHO_TOKEN" in str(excinfo.value)


def test_unknown_connector_names_are_rejected(settings):
    db = Database(settings.db_path); db.init()
    registry = make_registry(db)
    with pytest.raises(ConnectorError):
        registry.enable("nope")
    with pytest.raises(ConnectorError):
        registry.disable("nope")
    with pytest.raises(ConnectorError):
        registry.set_setting("nope", "default_target", "x")


def test_settings_persist_and_are_validated(settings):
    db = Database(settings.db_path); db.init()
    registry = make_registry(db)
    registry.set_setting("echo", "default_target", "#general")

    restarted = make_registry(db)
    assert restarted.get("echo").setting("default_target") == "#general"
    assert restarted.call("echo_target", {}) == "#general"

    with pytest.raises(ConnectorError) as excinfo:
        registry.set_setting("echo", "secret_token", "leak")
    assert "no setting" in str(excinfo.value)

    registry.set_setting("echo", "default_target", "")
    assert make_registry(db).get("echo").setting("default_target") == ""


def test_tool_specs_mark_write_tools_for_approval(settings):
    db = Database(settings.db_path); db.init()
    specs = make_registry(db).tool_specs()
    assert specs["echo_ping"]["approval"] is False
    assert specs["echo_write"]["approval"] is True
    assert specs["echo_write"]["args"] == {"text": "str (required)"}


def test_call_reports_disabled_connector_tools(settings):
    db = Database(settings.db_path); db.init()
    registry = make_registry(db)
    registry.disable("echo")
    with pytest.raises(ConnectorError) as excinfo:
        registry.call("echo_ping", {})
    assert "not available" in str(excinfo.value)


def test_call_wraps_errors_and_redacts_secrets(settings):
    db = Database(settings.db_path); db.init()
    secret = "echo-secret-token-123456"
    registry = make_registry(db, secrets=(secret,))
    with pytest.raises(ConnectorError) as excinfo:
        registry.call("echo_write", {})
    assert "needs text" in str(excinfo.value)

    class Boom(EchoConnector):
        def ping(self) -> str:
            raise RuntimeError(f"upstream said {secret}")

    db2 = Database(settings.db_path.parent / "b.sqlite3"); db2.init()
    registry2 = ConnectorRegistry(
        db2,
        env={"ECHO_TOKEN": secret},
        secrets=(secret,),
        classes=(Boom,),
    )
    with pytest.raises(ConnectorError) as excinfo2:
        registry2.call("echo_ping", {})
    assert secret not in str(excinfo2.value)
    assert "[REDACTED]" in str(excinfo2.value)


def test_status_reports_settings_and_tool_counts(settings):
    db = Database(settings.db_path); db.init()
    registry = make_registry(db)
    registry.set_setting("echo", "default_target", "x")
    row = registry.status()[0]
    assert row["name"] == "echo"
    assert row["tool_count"] == 3
    assert row["settings"] == {"default_target": "x"}
    assert [tool["name"] for tool in row["tools"]][:1] == ["echo_ping"]


# --------------------------------------------------------------------------
# real connectors against fake HTTP
# --------------------------------------------------------------------------
def test_github_lists_repos_and_reads_files():
    session = FakeSession([
        (200, [{"full_name": "a/b", "private": False, "default_branch": "main"}]),
        (200, {"path": "README.md", "size": 5, "content": "aGVsbG8="}),
    ])
    connector = GitHubConnector({"GITHUB_TOKEN": "ghp_x"}, {}, session=session)
    assert "a/b" in connector.list_repos()
    assert "hello" in connector.get_file("README.md", repo="a/b")
    assert session.calls[0]["headers"]["Authorization"] == "Bearer ghp_x"


def test_github_uses_saved_default_repo_and_rejects_bad_repo():
    session = FakeSession([(200, {"full_name": "a/b", "default_branch": "main",
                                  "language": "Python", "stargazers_count": 1,
                                  "open_issues_count": 0, "private": True})])
    connector = GitHubConnector({"GITHUB_TOKEN": "ghp_x"}, {"default_repo": "a/b"}, session=session)
    assert "a/b" in connector.get_repo()
    with pytest.raises(ConnectorError):
        connector.get_repo("../../etc")
    assert session.calls[-1]["url"].endswith("/repos/a/b")


def test_github_requires_repo_when_no_default_is_saved():
    connector = GitHubConnector({"GITHUB_TOKEN": "ghp_x"}, {}, session=FakeSession())
    with pytest.raises(ConnectorError) as excinfo:
        connector.get_repo()
    assert "default_repo" in str(excinfo.value)


def test_github_create_issue_posts_a_body():
    session = FakeSession([(201, {"number": 7, "html_url": "https://github.com/a/b/issues/7"})])
    connector = GitHubConnector({"GITHUB_TOKEN": "ghp_x"}, {"default_repo": "a/b"}, session=session)
    assert "#7" in connector.create_issue(title="Bug", body="details")
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["json"] == {"title": "Bug", "body": "details"}


def test_notion_accepts_a_copied_page_url():
    session = FakeSession([
        (200, {"properties": {}, "object": "page"}),
        (200, {"results": [{"paragraph": {"rich_text": [{"plain_text": "hello"}]}}]}),
    ])
    connector = NotionConnector({"NOTION_TOKEN": "ntn_x"}, {}, session=session)
    url = "https://www.notion.so/Team/My-Page-1234567890abcdef1234567890abcdef"
    assert "hello" in connector.get_page(url)
    assert session.calls[0]["url"].endswith("/pages/1234567890abcdef1234567890abcdef")


def test_notion_append_text_builds_paragraph_block():
    session = FakeSession([(200, {"results": [{"id": "b1"}]})])
    connector = NotionConnector({"NOTION_TOKEN": "ntn_x"}, {"default_page": "abc123"}, session=session)
    assert "Appended 1 block" in connector.append_text("note")
    block = session.calls[0]["json"]["children"][0]
    assert block["paragraph"]["rich_text"][0]["text"]["content"] == "note"


def test_slack_surfaces_api_errors():
    session = FakeSession([(200, {"ok": False, "error": "channel_not_found"})])
    connector = SlackConnector({"SLACK_BOT_TOKEN": "xoxb-x"}, {"default_channel": "#general"},
                               session=session)
    with pytest.raises(ConnectorError) as excinfo:
        connector.post_message("hi")
    assert "channel_not_found" in str(excinfo.value)


def test_cloudflare_purges_everything_or_listed_urls():
    session = FakeSession([(200, {"success": True}), (200, {"success": True})])
    connector = CloudflareConnector({"CLOUDFLARE_API_TOKEN": "cf"}, {"default_zone": "zone1"},
                                    session=session)
    assert "everything" in connector.purge_cache()
    assert session.calls[0]["json"] == {"purge_everything": True}
    connector.purge_cache(urls="https://a.example/x, https://a.example/y")
    assert session.calls[1]["json"]["files"] == ["https://a.example/x", "https://a.example/y"]


def test_render_trigger_deploy_posts_to_the_service():
    session = FakeSession([(201, {"id": "dep-1", "status": "created"})])
    connector = RenderConnector({"RENDER_API_KEY": "rnd"}, {"default_service": "srv-1"},
                                session=session)
    assert "dep-1" in connector.trigger_deploy()
    assert session.calls[0]["url"].endswith("/services/srv-1/deploys")


def test_connector_without_credentials_cannot_build_a_client():
    connector = GitHubConnector({}, {}, session=FakeSession())
    assert connector.configured is False
    with pytest.raises(ConnectorError):
        connector.list_repos()


# --------------------------------------------------------------------------
# Netlify
# --------------------------------------------------------------------------
def test_netlify_lists_sites_and_deploys():
    session = FakeSession([
        (200, [{"id": "site1", "name": "hermes-demo",
                "ssl_url": "https://hermes-demo.netlify.app",
                "build_settings": {"repo_url": "https://github.com/a/b"}}]),
        (200, [{"id": "dep1", "state": "ready", "branch": "main", "context": "production",
                "created_at": "2026-11-12T10:00:00Z",
                "deploy_ssl_url": "https://hermes-demo.netlify.app"}]),
    ])
    connector = NetlifyConnector({"NETLIFY_TOKEN": "nfp_x"}, {"default_site": "hermes-demo"},
                                 session=session)
    sites = connector.list_sites()
    assert "hermes-demo" in sites and "site1" in sites
    deploys = connector.list_deploys()
    assert "dep1" in deploys and "state=ready" in deploys
    assert session.calls[1]["url"].endswith("/sites/hermes-demo/deploys")


def test_netlify_site_detail_and_default_site_requirement():
    session = FakeSession([(200, {"id": "site1", "name": "demo",
                                  "ssl_url": "https://demo.netlify.app",
                                  "admin_url": "https://app.netlify.com/sites/demo",
                                  "build_settings": {"repo_url": "https://github.com/a/b",
                                                     "repo_branch": "main", "cmd": "npm run build",
                                                     "dir": "dist"}})])
    detail = NetlifyConnector({"NETLIFY_TOKEN": "nfp_x"}, {"default_site": "demo"},
                              session=session).get_site()
    assert "npm run build" in detail and "dist" in detail

    with pytest.raises(ConnectorError) as excinfo:
        NetlifyConnector({"NETLIFY_TOKEN": "nfp_x"}, {}, session=FakeSession()).get_site()
    assert "default_site" in str(excinfo.value)


def test_netlify_trigger_build_posts_to_the_site():
    session = FakeSession([(200, {"deploy_id": "dep2"})])
    connector = NetlifyConnector({"NETLIFY_TOKEN": "nfp_x"}, {"default_site": "demo"},
                                 session=session)
    assert "dep2" in connector.trigger_build()
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/sites/demo/builds")


def test_netlify_cancel_deploy_and_id_validation():
    session = FakeSession([(200, {})])
    connector = NetlifyConnector({"NETLIFY_TOKEN": "nfp_x"}, {}, session=session)
    assert "dep1" in connector.cancel_deploy("dep1")
    assert session.calls[0]["url"].endswith("/deploys/dep1/cancel")
    with pytest.raises(ConnectorError):
        connector.cancel_deploy("../../etc")


def test_netlify_write_tools_require_approval():
    connector = NetlifyConnector({"NETLIFY_TOKEN": "nfp_x"}, {}, session=FakeSession())
    writes = {tool.name for tool in connector.tools() if tool.write}
    assert writes == {"netlify_trigger_build", "netlify_cancel_deploy"}
