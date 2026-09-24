from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

from researchcloud.builders import build_create_network_payload, build_create_payload
from researchcloud.config import DEFAULT_CLOUD_NAME, DEFAULT_HOST_NAME_PREFIX
from researchcloud.utils.end_time import resolve_workspace_end_time, validate_workspace_end_time
from researchcloud.utils.filters import _matches_attribute_filters, _normalize_status_filter
from researchcloud.utils.flavours import match_size_flavour, validate_size_flavour_selection
from researchcloud.utils.naming import generate_host_name, generate_resource_name

if TYPE_CHECKING:
    from researchcloud.client import ResearchCloudClient


logger = logging.getLogger(__name__)
WORKSPACE_CREATE_TIMEOUT_SECONDS = 1800
WORKSPACE_CREATE_POLL_INTERVAL_SECONDS = 10


def _is_workspace_ready_status(status: str | None) -> bool:
    return status in {"available", "running", "in-use", "paused", "full"}


def _is_workspace_failure_status(status: str | None) -> bool:
    return status in {"failed", "unhealthy", "deleted", "deleting", "unknown", "unaccounted"}


@dataclass
class WorkspaceCreationPlan:
    """The fully-resolved resources and payload produced by `build_create_payload_from_names`."""

    co: dict
    wallet: dict
    catalog_item: dict
    offering: dict
    os_flavour: dict
    size_flavour: dict
    host_name: str
    end_time: str
    network_ref: dict | None
    payload: dict


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

        workspaces = await self._client._paginate("GET", "workspace", "workspaces/", params=params)

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

    async def resolve_network_and_offering(
        self,
        co_id: str,
        products: list,
        cloud_name: str,
        network_name_hint: str | None = None,
    ) -> tuple[dict, dict]:
        networks = await self._client.catalog.list_items_with_offerings(
            co_id,
            products,
            name=network_name_hint,
            application_type="Network",
        )
        if not networks:
            raise ValueError(
                "No network found"
                + (f" with name: {network_name_hint!r}." if network_name_hint else " for this CO/wallet.")
            )
        if len(networks) > 1:
            logger.warning("Multiple network entries found — using the first one: %r", networks[0]["name"])

        network = networks[0]
        offerings = await self._client.catalog.list_offerings_for_item(network["id"], co_id, products)
        if not offerings:
            raise ValueError(f"No offerings found for network {network['name']!r}.")

        network_cloud_name = self._client.to_network_cloud_name(cloud_name)
        cloud_offerings = [
            offering for offering in offerings if offering["subscription"]["name"] == network_cloud_name
        ]
        if not cloud_offerings:
            available = [offering["subscription"]["name"] for offering in offerings]
            logger.warning(
                "No network offering found for cloud %r (available: %s) — using the first available offering instead.",
                network_cloud_name,
                available,
            )
            cloud_offerings = offerings
        return network, cloud_offerings[0]

    async def create_network(
        self,
        co: dict,
        wallet: dict,
        products: list,
        cloud_name: str = DEFAULT_CLOUD_NAME,
        network_name: str = "",
        network_name_hint: str | None = None,
        network_description: str = "",
    ) -> str:
        network, offering = await self.resolve_network_and_offering(
            co["id"],
            products,
            cloud_name,
            network_name_hint,
        )
        payload = build_create_network_payload(co, wallet, network, offering, network_name, network_description)
        response = await self.create(payload)
        return response["id"]

    async def resolve_or_create_network(
        self,
        co: dict,
        wallet: dict,
        products: list,
        cloud_name: str = DEFAULT_CLOUD_NAME,
        network_name_hint: str | None = None,
        network_name_prefix: str = DEFAULT_HOST_NAME_PREFIX,
        dry_run: bool = False,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict:
        """Reuse an existing private network for this CO/cloud, or create one if none exists.

        Returns a network reference dict suitable for use as a workspace payload's network entry.
        """

        def report(message: str) -> None:
            if on_progress is not None:
                on_progress(message)

        report("Private network: looking for an existing one …")
        existing_networks = await self.list_networks(co["id"], cloud_name=cloud_name)
        if existing_networks:
            network = existing_networks[0]
            network_id = network["id"]
            report(f"Private network: reusing {network.get('name')!r}  (id: {network_id})")
            return {
                "id": network_id,
                "name": network.get("name") or network_id,
                "type": network.get("type") or "network",
            }

        if dry_run:
            report("Private network: none found — a new one would be created (skipped for dry run).")
            network_id = "<new-network-id>"
            return {"id": network_id, "name": network_id, "type": "network"}

        network_name = generate_resource_name(None, f"{network_name_prefix}-network")
        report(f"Private network: none found — creating {network_name!r} …")
        network_id = await self.create_network(
            co,
            wallet,
            products,
            cloud_name,
            network_name,
            network_name_hint=network_name_hint,
        )
        report(f"Private network: created (id: {network_id}), waiting until available …")
        network = await self.wait_for_network(network_id)
        report("Private network: available.")
        return {
            "id": network_id,
            "name": network.get("name") or network_name,
            "type": network.get("type") or "network",
        }

    async def build_create_payload_from_names(
        self,
        *,
        co_name: str,
        wallet_name: str,
        cloud_name: str,
        catalog_item_name: str,
        workspace_name: str,
        os_flavour_name: str,
        size_flavour_name: str | None = None,
        num_cpu: int | None = None,
        num_gpu: int | None = None,
        gpu_type: str | None = None,
        description: str = "",
        end_time: str | None = None,
        host_name: str | None = None,
        host_name_prefix: str = DEFAULT_HOST_NAME_PREFIX,
        network_name_hint: str | None = None,
        storage_ids: list[str | dict] | None = None,
        network_ids: list[str | dict] | None = None,
        ip_ids: list[str | dict] | None = None,
        dataset_names: list | None = None,
        dataset_ids: list | None = None,
        use_private_network: bool = False,
        optional_parameters: dict[str, str] | None = None,
        dry_run: bool = False,
        on_progress: Callable[[str], None] | None = None,
    ) -> WorkspaceCreationPlan:
        """Resolve all names/conveniences (CO, wallet, catalog item, flavours, host name,
        end time, and optionally a private network) into a fully-built workspace create
        payload, without submitting it. Use this to build a payload for `create()` from
        user-friendly inputs instead of assembling one by hand.
        """

        def report(message: str) -> None:
            if on_progress is not None:
                on_progress(message)

        validate_size_flavour_selection(size_flavour_name, num_cpu, num_gpu)
        selected_host_name = generate_host_name(host_name, host_name_prefix)
        selected_end_time = resolve_workspace_end_time(end_time)
        validate_workspace_end_time(selected_end_time)

        co, wallet = await asyncio.gather(
            self._client.resolve_co(co_name),
            self._client.resolve_wallet(wallet_name),
        )
        products = wallet["budgets"][0]["products"]
        report(f"CO          : {co['co_name']}  (id: {co['id']})")
        report(f"Wallet      : {wallet['name']}  (id: {wallet['id']})")
        report(f"Products    : {products}")

        catalog_item = await self._client.resolve_catalog_item(catalog_item_name, co["id"], products)
        report(f"Catalog item: {catalog_item['name']}  (id: {catalog_item['id']})")

        resolved_size_name = None if (num_cpu is not None or num_gpu is not None) else size_flavour_name
        offering, size_flavour, os_flavour = await self._client.resolve_offering_and_flavours(
            catalog_item,
            co["id"],
            products,
            cloud_name,
            os_flavour_name,
            resolved_size_name,
        )
        if num_cpu is not None or num_gpu is not None:
            size_flavour = match_size_flavour(
                offering.get("flavours", []),
                num_cpu=num_cpu,
                num_gpu=num_gpu,
                gpu_type=gpu_type,
            )
        if size_flavour is None:
            raise ValueError("Could not resolve a size flavour for workspace creation.")
        report(f"Cloud       : {offering['subscription']['name']}")
        report(f"OS flavour  : {os_flavour['name']}")
        report(f"Size flavour: {size_flavour['name']}")
        report(f"Host name   : {selected_host_name}")
        if optional_parameters:
            report(f"Optional parameters supplied: {sorted(optional_parameters.keys())}")

        attached_network_ids: list[str | dict] = list(network_ids or [])
        network_ref: dict | None = None
        if use_private_network:
            network_ref = await self.resolve_or_create_network(
                co,
                wallet,
                products,
                cloud_name=cloud_name,
                network_name_hint=network_name_hint,
                network_name_prefix=host_name_prefix,
                dry_run=dry_run,
                on_progress=on_progress,
            )
            attached_network_ids = [network_ref]

        payload = build_create_payload(
            co=co,
            wallet=wallet,
            catalog_item=catalog_item,
            offering=offering,
            os_flavour=os_flavour,
            size_flavour=size_flavour,
            workspace_name=workspace_name,
            workspace_description=description,
            end_time=selected_end_time,
            host_name=selected_host_name,
            storage_ids=storage_ids or [],
            network_ids=attached_network_ids,
            ip_ids=ip_ids or [],
            dataset_names=dataset_names or [],
            dataset_ids=dataset_ids or [],
            optional_parameters=optional_parameters,
        )

        if optional_parameters:
            self._client.validate_optional_parameters(offering, optional_parameters)

        return WorkspaceCreationPlan(
            co=co,
            wallet=wallet,
            catalog_item=catalog_item,
            offering=offering,
            os_flavour=os_flavour,
            size_flavour=size_flavour,
            host_name=selected_host_name,
            end_time=selected_end_time,
            network_ref=network_ref,
            payload=payload,
        )

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
