from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from researchcloud.client import ResearchCloudClient
from researchcloud.errors import ApiError, TransportError


class DummyResponse:
    def __init__(self, status: int, body, content_type: str = "application/json", headers: dict | None = None):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type, **(headers or {})}
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


def test_validate_optional_parameters_allows_expected_keys():
    client = ResearchCloudClient(token="token", session=DummySession())

    client.validate_optional_parameters(
        {"optional_parameters": {"username": {}, "password": {}}},
        {"username": "alice"},
    )


def test_validate_optional_parameters_rejects_unexpected_keys():
    client = ResearchCloudClient(token="token", session=DummySession())

    with pytest.raises(ValueError, match="Unsupported optional parameter keys"):
        client.validate_optional_parameters(
            {"optional_parameters": {"username": {}}},
            {"unexpected": "value"},
        )


def test_validate_optional_parameters_skips_when_none_supplied():
    client = ResearchCloudClient(token="token", session=DummySession())

    client.validate_optional_parameters({"optional_parameters": {"username": {}}}, None)


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


def test_request_retries_on_429_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", _record_sleep(sleeps))

    session = DummySession([
        DummyResponse(429, {"detail": "rate limited"}, headers={"Retry-After": "0.01"}),
        DummyResponse(200, {"ok": True}),
    ])
    client = ResearchCloudClient(token="token", session=session)

    result = _run(client.request("GET", "workspace", "workspaces/"))

    assert result == {"ok": True}
    assert len(session.calls) == 2
    assert sleeps == [0.01]


def test_request_retries_on_5xx_with_exponential_backoff(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", _record_sleep(sleeps))
    monkeypatch.setattr("researchcloud.client.random.uniform", lambda a, b: 0.0)

    session = DummySession([
        DummyResponse(503, "service unavailable", content_type="text/plain"),
        DummyResponse(502, "bad gateway", content_type="text/plain"),
        DummyResponse(200, {"ok": True}),
    ])
    client = ResearchCloudClient(token="token", session=session, backoff_base_seconds=1.0, backoff_max_seconds=10.0)

    result = _run(client.request("GET", "workspace", "workspaces/"))

    assert result == {"ok": True}
    assert len(session.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_request_raises_api_error_after_exhausting_retries(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", _record_sleep(sleeps))

    session = DummySession([DummyResponse(503, "down", content_type="text/plain") for _ in range(4)])
    client = ResearchCloudClient(token="token", session=session, max_retries=3)

    with pytest.raises(ApiError, match="HTTP 503"):
        _run(client.request("GET", "workspace", "workspaces/"))

    assert len(session.calls) == 4
    assert len(sleeps) == 3


def test_request_does_not_retry_non_transient_client_errors(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", _record_sleep(sleeps))

    session = DummySession([DummyResponse(404, {"message": "not found"})])
    client = ResearchCloudClient(token="token", session=session)

    with pytest.raises(ApiError, match="HTTP 404"):
        _run(client.request("GET", "workspace", "workspaces/ws-1/"))

    assert len(session.calls) == 1
    assert sleeps == []


def _record_sleep(sleeps: list[float]):
    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    return fake_sleep


def test_paginate_collects_all_pages_and_advances_offset():
    session = DummySession([
        DummyResponse(200, {"results": [{"id": "a"}, {"id": "b"}], "next": "?offset=2&limit=2"}),
        DummyResponse(200, {"results": [{"id": "c"}], "next": None}),
    ])
    client = ResearchCloudClient(token="token", session=session)

    result = _run(client._paginate("GET", "workspace", "workspaces/", params={"co_id": "co-1"}, page_size=2))

    assert [item["id"] for item in result] == ["a", "b", "c"]
    assert session.calls[0]["params"] == {"co_id": "co-1", "limit": 2, "offset": 0}
    assert session.calls[1]["params"] == {"co_id": "co-1", "limit": 2, "offset": 2}


def test_paginate_stops_on_empty_page_even_if_next_is_set():
    session = DummySession([
        DummyResponse(200, {"results": [], "next": "?offset=0&limit=100"}),
    ])
    client = ResearchCloudClient(token="token", session=session)

    result = _run(client._paginate("GET", "workspace", "workspaces/"))

    assert result == []
    assert len(session.calls) == 1
