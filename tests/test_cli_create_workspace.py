from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from researchcloud import cli
from researchcloud.services.workspaces import WorkspaceCreationPlan


def _run(coro):
    return asyncio.run(coro)


PLAN = WorkspaceCreationPlan(
    co={"id": "co-1", "co_name": "Example CO"},
    wallet={"id": "wallet-1", "name": "Example Wallet"},
    catalog_item={"id": "catalog-1", "name": "My App"},
    offering={"id": "offering-1", "subscription": {"name": "SURF HPC Cloud"}},
    os_flavour={"name": "Ubuntu 22.04"},
    size_flavour={"name": "8 Core - 32 GB"},
    host_name="ws-test",
    end_time="2099-01-01T00:00:00Z",
    network_ref=None,
    payload={"name": "my-workspace", "meta": {}},
)


class _FakeClient:
    def __init__(self):
        self.workspaces = AsyncMock()
        self.workspaces.build_create_payload_from_names = AsyncMock(return_value=PLAN)
        self.workspaces.create = AsyncMock(return_value={"id": "ws-1"})
        self.workspaces.wait_until_ready = AsyncMock(return_value={"id": "ws-1", "status": "running"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_create_workspace_dry_run_builds_payload_via_service(monkeypatch, capsys):
    fake_client = _FakeClient()
    monkeypatch.setenv("RESEARCH_CLOUD_TOKEN", "token")

    with patch.object(cli.ResearchCloudClient, "from_env", return_value=fake_client):
        _run(
            cli.create_workspace(
                co_name="Example CO",
                wallet_name="Example Wallet",
                cloud_name="SURF HPC Cloud",
                catalog_item_name="My App",
                workspace_name="my-workspace",
                os_flavour_name="Ubuntu 22.04",
                size_flavour_name="8 Core - 32 GB",
                dry_run=True,
            )
        )

    fake_client.workspaces.build_create_payload_from_names.assert_awaited_once()
    call_kwargs = fake_client.workspaces.build_create_payload_from_names.call_args.kwargs
    assert call_kwargs["dry_run"] is True
    assert call_kwargs["co_name"] == "Example CO"
    fake_client.workspaces.create.assert_not_awaited()
    assert "my-workspace" in capsys.readouterr().out


def test_create_workspace_creates_and_waits_when_not_dry_run(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setenv("RESEARCH_CLOUD_TOKEN", "token")

    with patch.object(cli.ResearchCloudClient, "from_env", return_value=fake_client):
        _run(
            cli.create_workspace(
                co_name="Example CO",
                wallet_name="Example Wallet",
                cloud_name="SURF HPC Cloud",
                catalog_item_name="My App",
                workspace_name="my-workspace",
                os_flavour_name="Ubuntu 22.04",
                size_flavour_name="8 Core - 32 GB",
                dry_run=False,
            )
        )

    fake_client.workspaces.create.assert_awaited_once_with(PLAN.payload)
    fake_client.workspaces.wait_until_ready.assert_awaited_once()


def test_main_exits_when_token_missing(monkeypatch):
    monkeypatch.delenv("RESEARCH_CLOUD_TOKEN", raising=False)
    monkeypatch.setattr(cli.sys, "argv", ["researchcloud", "delete-workspace", "--id", "ws-1"])

    with pytest.raises(SystemExit):
        cli.main()


class _FakeWorkspaceLifecycleClient:
    def __init__(self):
        self.workspaces = AsyncMock()
        self.workspaces.get = AsyncMock(return_value={"id": "ws-1", "status": "running"})
        self.workspaces.pause = AsyncMock(return_value={"id": "ws-1", "status": "pausing"})
        self.workspaces.resume = AsyncMock(return_value={"id": "ws-1", "status": "resuming"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_get_workspace_status_prints_status(monkeypatch, capsys):
    fake_client = _FakeWorkspaceLifecycleClient()
    monkeypatch.setenv("RESEARCH_CLOUD_TOKEN", "token")

    with patch.object(cli.ResearchCloudClient, "from_env", return_value=fake_client):
        _run(cli.get_workspace_status(workspace_id="ws-1"))

    fake_client.workspaces.get.assert_awaited_once_with("ws-1")
    assert capsys.readouterr().out.strip() == "running"


def test_get_workspace_status_dry_run_skips_request(monkeypatch, capsys):
    fake_client = _FakeWorkspaceLifecycleClient()
    monkeypatch.setenv("RESEARCH_CLOUD_TOKEN", "token")

    with patch.object(cli.ResearchCloudClient, "from_env", return_value=fake_client):
        _run(cli.get_workspace_status(workspace_id="ws-1", dry_run=True))

    fake_client.workspaces.get.assert_not_awaited()
    assert "ws-1" in capsys.readouterr().out


def test_pause_workspace_by_id_calls_pause(monkeypatch, capsys):
    fake_client = _FakeWorkspaceLifecycleClient()
    monkeypatch.setenv("RESEARCH_CLOUD_TOKEN", "token")

    with patch.object(cli.ResearchCloudClient, "from_env", return_value=fake_client):
        _run(cli.pause_workspace_by_id(workspace_id="ws-1"))

    fake_client.workspaces.pause.assert_awaited_once_with("ws-1")
    assert "Paused workspace 'ws-1'" in capsys.readouterr().out


def test_resume_workspace_by_id_calls_resume(monkeypatch, capsys):
    fake_client = _FakeWorkspaceLifecycleClient()
    monkeypatch.setenv("RESEARCH_CLOUD_TOKEN", "token")

    with patch.object(cli.ResearchCloudClient, "from_env", return_value=fake_client):
        _run(cli.resume_workspace_by_id(workspace_id="ws-1"))

    fake_client.workspaces.resume.assert_awaited_once_with("ws-1")
    assert "Resumed workspace 'ws-1'" in capsys.readouterr().out


def test_build_parser_dispatches_lifecycle_commands():
    parser = cli.build_parser()

    status_args = parser.parse_args(["get-workspace-status", "--id", "ws-1"])
    assert status_args.handler is cli.get_workspace_status
    assert status_args.workspace_id == "ws-1"

    pause_args = parser.parse_args(["pause-workspace", "--id", "ws-1"])
    assert pause_args.handler is cli.pause_workspace_by_id

    resume_args = parser.parse_args(["resume-workspace", "--id", "ws-1"])
    assert resume_args.handler is cli.resume_workspace_by_id
