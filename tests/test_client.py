from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from researchcloud.client import ResearchCloudClient
from researchcloud.errors import ApiError, TransportError


class DummyResponse:
    def __init__(self, status: int, body, content_type: str = "application/json"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}
        self.ok = 200 <= status < 300

    async def json(self):
        return self._body

    async def text(self):
        if isinstance(self._body, str):
            return self._body
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummySession:
    def __init__(self, responses=None, exc: Exception | None = None):
        self.responses = list(responses or [])
        self.exc = exc
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, params=None, json=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        if self.exc is not None:
            raise self.exc
        if not self.responses:
            raise AssertionError("No prepared response available for request.")
        return self.responses.pop(0)


def _run(coro):
    return asyncio.run(coro)


def test_request_returns_json_body():
    session = DummySession([DummyResponse(200, {"ok": True})])
    client = ResearchCloudClient(token="token", session=session)

    result = _run(client.request("GET", "workspace", "workspaces/"))

    assert result == {"ok": True}
    assert session.calls == [{
        "method": "GET",
        "url": "https://gw.live.surfresearchcloud.nl/v1/workspace/workspaces/",
        "params": None,
        "json": None,
    }]


def test_request_uses_client_base_url_overrides():
    session = DummySession([DummyResponse(200, {"ok": True})])
    client = ResearchCloudClient(
        token="token",
        workspace_base_url="https://workspace.example/api/",
        session=session,
    )

    result = _run(client.request("GET", "workspace", "workspaces/"))

    assert result == {"ok": True}
    assert session.calls[0]["url"] == "https://workspace.example/api/workspaces/"


def test_request_returns_text_body():
    session = DummySession([DummyResponse(204, "", content_type="text/plain")])
    client = ResearchCloudClient(token="token", session=session)

    result = _run(client.request("DELETE", "workspace", "workspaces/ws-1/"))

    assert result == ""


def test_request_raises_api_error_for_non_success_status():
    session = DummySession([DummyResponse(404, {"message": ["not found"]})])
    client = ResearchCloudClient(token="token", session=session)

    with pytest.raises(ApiError, match="HTTP 404"):
        _run(client.request("GET", "workspace", "workspaces/ws-1/"))


def test_request_raises_transport_error_for_client_failure():
    session = DummySession(exc=aiohttp.ClientConnectionError("boom"))
    client = ResearchCloudClient(token="token", session=session)

    with pytest.raises(TransportError, match="boom"):
        _run(client.request("GET", "workspace", "workspaces/"))


def test_from_env_reads_client_configuration(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_TOKEN", "env-token")
    monkeypatch.setenv("WORKSPACE_BASE_URL", "https://workspace.example/api/")

    client = ResearchCloudClient.from_env(session=DummySession())

    assert client.token == "env-token"
    assert client.workspace_base_url == "https://workspace.example/api/"


def test_client_requires_token_when_creating_owned_session():
    client = ResearchCloudClient()

    with pytest.raises(ValueError, match="RESEARCH_CLOUD_TOKEN is required"):
        _run(client._ensure_session())


def test_resolve_co_uses_client_directly():
    session = DummySession([
        DummyResponse(200, {"COs": [{"id": "co-1", "co_name": "Example CO"}]}),
    ])
    client = ResearchCloudClient(token="token", session=session)

    result = _run(client.resolve_co("Example CO"))

    assert result == {"id": "co-1", "co_name": "Example CO"}
    assert session.calls[0]["url"].endswith("/users/self/")


def test_expected_optional_parameter_keys_support_mapping_shape():
    client = ResearchCloudClient(token="token", session=DummySession())

    result = client.get_expected_optional_parameter_keys(
        {"optional_parameters": {"username": {}, "password": {}}}
    )

    assert result == ("username", "password")


def test_workspace_list_filters_multiple_statuses_client_side():
    session = DummySession([
        DummyResponse(
            200,
            {
                "results": [
                    {"id": "ws-1", "status": "running", "meta": {}, "time_created": "2024-03-01"},
                    {"id": "ws-2", "status": "paused", "meta": {}, "time_created": "2024-02-01"},
                    {"id": "ws-3", "status": "creating", "meta": {}, "time_created": "2024-01-01"},
                ],
                "next": None,
            },
        )
    ])
    client = ResearchCloudClient(token="token", session=session)

    result = _run(
        client.workspaces.list(
            co_id="co-1",
            catalog_item_name="",
            status=("running", "paused"),
        )
    )

    assert [workspace["id"] for workspace in result] == ["ws-1", "ws-2"]
    assert session.calls[0]["params"] == {
        "co_id": "co-1",
        "application_type": "Compute",
        "deleted": "false",
        "limit": 100,
        "offset": 0,
    }


def test_workspace_list_filters_nested_attributes():
    session = DummySession([
        DummyResponse(
            200,
            {
                "results": [
                    {
                        "id": "ws-1",
                        "status": "running",
                        "time_created": "2024-03-01",
                        "meta": {
                            "application_name": "Ray Head Node",
                            "interactive_parameters": [
                                {"key": "username", "value": "alice"},
                            ],
                        },
                    },
                    {
                        "id": "ws-2",
                        "status": "running",
                        "time_created": "2024-02-01",
                        "meta": {
                            "application_name": "Ray Head Node",
                            "interactive_parameters": [
                                {"key": "username", "value": "bob"},
                            ],
                        },
                    },
                ],
                "next": None,
            },
        )
    ])
    client = ResearchCloudClient(token="token", session=session)

    result = _run(
        client.workspaces.list(
            co_id="co-1",
            catalog_item_name="Ray Head Node",
            attribute_filters={
                "meta.interactive_parameters": [
                    {"key": "username", "value": "alice"},
                ],
            },
        )
    )

    assert [workspace["id"] for workspace in result] == ["ws-1"]
