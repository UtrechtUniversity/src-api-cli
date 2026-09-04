from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

from researchcloud.config import DEFAULT_CLOUD_NAME
from researchcloud.utils.filters import _matches_attribute_filters, _normalize_status_filter

if TYPE_CHECKING:
    from researchcloud.client import ResearchCloudClient


logger = logging.getLogger(__name__)
WORKSPACE_CREATE_TIMEOUT_SECONDS = 1800
WORKSPACE_CREATE_POLL_INTERVAL_SECONDS = 10


def _is_workspace_ready_status(status: str | None) -> bool:
    return status in {"available", "running", "in-use", "paused", "full"}


def _is_workspace_failure_status(status: str | None) -> bool:
    return status in {"failed", "unhealthy", "deleted", "deleting", "unknown", "unaccounted"}


class WorkspacesService:
    def __init__(self, client: ResearchCloudClient):
        self._client = client

    async def list(
        self,
        co_id: str,
        catalog_item_name: str,
        by_owner: bool = False,
        application_type: str = "Compute",
        workspace_name: str | None = None,
        status: str | Sequence[str] | None = None,
        attribute_filters: Mapping[str, object] | None = None,
    ) -> list:
        params: dict[str, object] = {
            "co_id": co_id,
            "application_type": application_type,
            "deleted": "false",
            "limit": 100,
        }
        if by_owner:
            params["by_owner"] = "true"
        normalized_statuses = _normalize_status_filter(status)
        if len(normalized_statuses) == 1:
            params["status"] = normalized_statuses[0]

        workspaces: list[dict] = []
        offset = 0
        while True:
            params["offset"] = offset
            response = await self._client.request("GET", "workspace", "workspaces/", params=params)
            page = response.get("results", [])
            workspaces.extend(page)
            if response.get("next") is None:
                break
            offset += len(page)

        if catalog_item_name:
            result = [
                workspace
                for workspace in workspaces
                if workspace.get("meta", {}).get("application_name") == catalog_item_name
            ]
        else:
            result = workspaces

        if workspace_name:
            result = [workspace for workspace in result if workspace.get("name") == workspace_name]

        if len(normalized_statuses) > 1:
            allowed_statuses = set(normalized_statuses)
            result = [workspace for workspace in result if workspace.get("status") in allowed_statuses]

        if attribute_filters:
            result = [workspace for workspace in result if _matches_attribute_filters(workspace, attribute_filters)]

        result.sort(key=lambda workspace: workspace.get("time_created", ""), reverse=True)
        return result

    async def list_networks(
        self,
        co_id: str,
        cloud_name: str = DEFAULT_CLOUD_NAME,
        by_owner: bool = False,
    ) -> list:
        networks = await self.list(
            co_id=co_id,
            catalog_item_name="",
            by_owner=by_owner,
            application_type="Network",
        )
        return [
            network
            for network in networks
            if network.get("meta", {}).get("subscription_name") == f"{cloud_name} Network"
        ]

    async def get(self, workspace_id: str) -> dict:
        return await self._client.request("GET", "workspace", f"workspaces/{quote_plus(workspace_id)}/")

    async def create(self, payload: dict) -> dict:
        return await self._client.request("POST", "workspace", "workspaces/", data=payload)

    async def delete(self, workspace_id: str) -> None:
        await self._client.request("DELETE", "workspace", f"workspaces/{quote_plus(workspace_id)}/")

    async def trigger_action(self, workspace_id: str, action_type: str) -> dict:
        normalized_action = action_type.strip().lower()
        return await self._client.request(
            "POST",
            "workspace",
            f"workspaces/{quote_plus(workspace_id)}/actions/{quote_plus(normalized_action)}/",
            data={},
        )

    async def pause(self, workspace_id: str) -> dict:
        return await self.trigger_action(workspace_id, "pause")

    async def resume(self, workspace_id: str) -> dict:
        return await self.trigger_action(workspace_id, "resume")

    async def is_running(self, workspace_id: str) -> bool:
        workspace = await self.get(workspace_id)
        return workspace.get("status") == "running"

    async def wait_until_ready(
        self,
        workspace_id: str,
        timeout: float = WORKSPACE_CREATE_TIMEOUT_SECONDS,
        poll_interval: float = WORKSPACE_CREATE_POLL_INTERVAL_SECONDS,
        status_callback: Callable[[str | None, float], None] | None = None,
    ) -> dict:
        elapsed = 0.0
        last_status: str | None = None

        while True:
            workspace = await self.get(workspace_id)
            workspace_status = workspace.get("status")
            if workspace_status != last_status:
                if status_callback is not None:
                    status_callback(workspace_status, elapsed)
                last_status = workspace_status

            if _is_workspace_ready_status(workspace_status):
                return workspace
            if _is_workspace_failure_status(workspace_status):
                raise RuntimeError(
                    f"Workspace {workspace_id} entered failure status {workspace_status!r}: {workspace}"
                )
            if elapsed >= timeout:
                raise TimeoutError(
                    f"Timed out after {int(timeout)}s waiting for workspace {workspace_id} to become ready."
                )

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

    async def wait_for_network(
        self,
        network_id: str,
        timeout: float = 300,
        poll_interval: float = 5,
    ) -> dict:
        elapsed = 0.0
        failure_statuses = {"failed", "unhealthy", "deleted"}

        while True:
            network = await self.get(network_id)
            network_status = network.get("status")
            logger.info("Network %s status: %s", network_id, network_status)
            if network_status == "available":
                return network
            if network_status in failure_statuses:
                raise RuntimeError(f"Network {network_id} entered failure status {network_status!r}: {network}")
            if elapsed >= timeout:
                raise TimeoutError(
                    f"Timed out after {timeout}s waiting for network {network_id} to become available."
                )

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
