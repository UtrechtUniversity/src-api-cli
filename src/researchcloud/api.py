from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from urllib.parse import quote_plus, urljoin

import aiohttp

from researchcloud.builders import build_create_network_payload, build_create_payload
from researchcloud.client import ResearchCloudClient
from researchcloud.config import (
    DEFAULT_CATALOG_BASE_URL,
    DEFAULT_CLOUD_NAME,
    DEFAULT_USER_BASE_URL,
    DEFAULT_WALLET_BASE_URL,
    DEFAULT_WORKSPACE_BASE_URL,
)
from researchcloud.services.workspaces import (
    WORKSPACE_CREATE_POLL_INTERVAL_SECONDS,
    WORKSPACE_CREATE_TIMEOUT_SECONDS,
    _is_workspace_failure_status,
    _is_workspace_ready_status,
)
from researchcloud.utils.filters import (
    _get_nested_attribute,
    _matches_attribute_filters,
    _matches_expected_value,
    _normalize_status_filter,
)
from researchcloud.utils.flavours import _parse_size_flavour, match_size_flavour


logger = logging.getLogger(__name__)
CATALOG_BASE_URL = DEFAULT_CATALOG_BASE_URL
USER_BASE_URL = DEFAULT_USER_BASE_URL
WALLET_BASE_URL = DEFAULT_WALLET_BASE_URL
WORKSPACE_BASE_URL = DEFAULT_WORKSPACE_BASE_URL


def to_network_cloud_name(cloud_name: str) -> str:
    normalized = cloud_name.strip()
    if normalized.endswith(" Network"):
        return normalized
    return f"{normalized} Network"


async def make_request(
    session: aiohttp.ClientSession,
    method: str,
    base_url: str,
    path: str = "",
    params=None,
    data=None,
):
    url = urljoin(base_url, path)
    logger.info("%-6s %s  params=%s", method, url, params)

    try:
        async with session.request(method, url, params=params, json=data) as response:
            content_type = response.headers.get("Content-Type", "")
            body = await response.json() if "application/json" in content_type else await response.text()
            if not response.ok:
                error_body = body if isinstance(body, dict) else {"message": [body]}
                logger.error("HTTP %s for %s — %s", response.status, url, error_body.get("message", error_body))
                return response.status, error_body
            return response.status, body
    except aiohttp.ClientError as exc:
        logger.error("Request failed for %s — %s", url, exc)
        return 500, {"message": [str(exc)]}


def _coerce_client(session_or_client: aiohttp.ClientSession | ResearchCloudClient) -> ResearchCloudClient:
    if isinstance(session_or_client, ResearchCloudClient):
        return session_or_client
    return ResearchCloudClient(session=session_or_client)


async def get_wallets(session_or_client: aiohttp.ClientSession | ResearchCloudClient, name: str | None = None) -> list:
    return await _coerce_client(session_or_client).wallets.list(name)


async def get_user_cos(session_or_client: aiohttp.ClientSession | ResearchCloudClient, name: str | None = None) -> list:
    return await _coerce_client(session_or_client).users.list_cos(name)


async def get_catalog_items_with_offerings(
    session_or_client: aiohttp.ClientSession | ResearchCloudClient,
    co_id: str,
    products: list,
    name: str | None = None,
    application_type: str | None = "Compute",
) -> list:
    return await _coerce_client(session_or_client).catalog.list_items_with_offerings(
        co_id=co_id,
        products=products,
        name=name,
        application_type=application_type,
    )


async def get_offerings_for_item(
    session_or_client: aiohttp.ClientSession | ResearchCloudClient,
    catalog_item_id: str,
    co_id: str,
    products: list,
) -> list:
    return await _coerce_client(session_or_client).catalog.list_offerings_for_item(catalog_item_id, co_id, products)


async def get_workspaces(
    session_or_client: aiohttp.ClientSession | ResearchCloudClient,
    co_name: str,
    catalog_item_name: str,
    by_owner: bool = False,
    application_type: str = "Compute",
    workspace_name: str | None = None,
    status: str | Sequence[str] | None = None,
    attribute_filters: Mapping[str, object] | None = None,
) -> list:
    co = await resolve_co(session_or_client, co_name)
    return await _coerce_client(session_or_client).workspaces.list(
        co_id=co["id"],
        catalog_item_name=catalog_item_name,
        by_owner=by_owner,
        application_type=application_type,
        workspace_name=workspace_name,
        status=status,
        attribute_filters=attribute_filters,
    )


async def get_networks(
    session_or_client: aiohttp.ClientSession | ResearchCloudClient,
    co_id: str,
    cloud_name: str = DEFAULT_CLOUD_NAME,
    by_owner: bool = False,
) -> list:
    return await _coerce_client(session_or_client).workspaces.list_networks(
        co_id=co_id,
        cloud_name=cloud_name,
        by_owner=by_owner,
    )


async def get_workspace(session_or_client: aiohttp.ClientSession | ResearchCloudClient, workspace_id: str) -> dict:
    return await _coerce_client(session_or_client).workspaces.get(workspace_id)


async def trigger_workspace_action(
    session_or_client: aiohttp.ClientSession | ResearchCloudClient,
    workspace_id: str,
    action_type: str,
) -> dict:
    return await _coerce_client(session_or_client).workspaces.trigger_action(workspace_id, action_type)


async def pause_workspace(session_or_client: aiohttp.ClientSession | ResearchCloudClient, workspace_id: str) -> dict:
    return await _coerce_client(session_or_client).workspaces.pause(workspace_id)


async def resume_workspace(session_or_client: aiohttp.ClientSession | ResearchCloudClient, workspace_id: str) -> dict:
    return await _coerce_client(session_or_client).workspaces.resume(workspace_id)


async def is_workspace_running(session_or_client: aiohttp.ClientSession | ResearchCloudClient, workspace_id: str) -> bool:
    return await _coerce_client(session_or_client).workspaces.is_running(workspace_id)


async def delete_workspace(session_or_client: aiohttp.ClientSession | ResearchCloudClient, workspace_id: str) -> None:
    await _coerce_client(session_or_client).workspaces.delete(workspace_id)


async def resolve_wallet(session_or_client: aiohttp.ClientSession | ResearchCloudClient, wallet_name: str) -> dict:
    matches = await get_wallets(session_or_client, wallet_name)
    if not matches:
        raise ValueError(f"No wallet found with name: {wallet_name!r}")
    if len(matches) > 1:
        logger.warning("Multiple wallets match %r — using the first one.", wallet_name)
    return matches[0]


async def resolve_co(session_or_client: aiohttp.ClientSession | ResearchCloudClient, co_name: str) -> dict:
    matches = await get_user_cos(session_or_client, co_name)
    if not matches:
        raise ValueError(f"No CO found with name: {co_name!r}")
    if len(matches) > 1:
        logger.warning("Multiple COs match %r — using the first one.", co_name)
    return matches[0]


async def resolve_catalog_item(
    session_or_client: aiohttp.ClientSession | ResearchCloudClient,
    catalog_item_name: str,
    co_id: str,
    products: list,
) -> dict:
    matches = await get_catalog_items_with_offerings(session_or_client, co_id, products, catalog_item_name)
    if not matches:
        raise ValueError(
            f"No catalog item (Application Offering) found with name: {catalog_item_name!r}. "
            "Check the provided CO, wallet product scope, and catalog item name."
        )
    if len(matches) > 1:
        logger.warning("Multiple catalog items match %r — using the first one.", catalog_item_name)
    return matches[0]


async def resolve_offering_and_flavours(
    session_or_client: aiohttp.ClientSession | ResearchCloudClient,
    catalog_item: dict,
    co_id: str,
    products: list,
    cloud_name: str,
    os_flavour_name: str,
    size_flavour_name: str | None,
) -> tuple[dict, dict | None, dict]:
    offerings = await get_offerings_for_item(session_or_client, catalog_item["id"], co_id, products)
    cloud_offerings = [offering for offering in offerings if offering["subscription"]["name"] == cloud_name]
    if not cloud_offerings:
        available = [offering["subscription"]["name"] for offering in offerings]
        raise ValueError(f"No offering found for cloud {cloud_name!r}. Available cloud subscriptions: {available}")

    offering = cloud_offerings[0]
    flavours = offering.get("flavours", [])
    os_flavours = [flavour for flavour in flavours if flavour["name"] == os_flavour_name]
    if not os_flavours:
        available_os = [flavour["name"] for flavour in flavours if flavour.get("category") == "os"]
        raise ValueError(f"OS flavour {os_flavour_name!r} not found. Available OS flavours: {available_os}")

    if size_flavour_name is None:
        return offering, None, os_flavours[0]

    size_flavours = [flavour for flavour in flavours if flavour["name"] == size_flavour_name]
    if not size_flavours:
        available_sizes = [flavour["name"] for flavour in flavours if flavour.get("category") == "size"]
        raise ValueError(
            f"Size flavour {size_flavour_name!r} not found. Available size flavours: {available_sizes}"
        )
    return offering, size_flavours[0], os_flavours[0]


async def resolve_network_and_offering(
    session_or_client: aiohttp.ClientSession | ResearchCloudClient,
    co_id: str,
    products: list,
    cloud_name: str,
    network_name_hint: str | None = None,
) -> tuple[dict, dict]:
    networks = await get_catalog_items_with_offerings(
        session_or_client,
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
    offerings = await get_offerings_for_item(session_or_client, network["id"], co_id, products)
    if not offerings:
        raise ValueError(f"No offerings found for network {network['name']!r}.")

    network_cloud_name = to_network_cloud_name(cloud_name)
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


def get_expected_optional_parameter_keys(offering: Mapping[str, object]) -> tuple[str, ...]:
    raw_optional_parameters = offering.get("optional_parameters")
    if isinstance(raw_optional_parameters, Mapping):
        return tuple(key for key in raw_optional_parameters.keys() if isinstance(key, str))
    if isinstance(raw_optional_parameters, Sequence) and not isinstance(
        raw_optional_parameters, (str, bytes, bytearray)
    ):
        keys: list[str] = []
        for item in raw_optional_parameters:
            if isinstance(item, Mapping):
                for candidate_key in ("key", "name"):
                    value = item.get(candidate_key)
                    if isinstance(value, str) and value:
                        keys.append(value)
                        break
        return tuple(keys)
    return ()


async def create_network(
    session_or_client: aiohttp.ClientSession | ResearchCloudClient,
    co: dict,
    wallet: dict,
    products: list,
    cloud_name: str,
    network_name: str,
    network_name_hint: str | None = None,
    network_description: str = "",
) -> str:
    client = _coerce_client(session_or_client)
    network, offering = await resolve_network_and_offering(client, co["id"], products, cloud_name, network_name_hint)
    payload = build_create_network_payload(co, wallet, network, offering, network_name, network_description)
    response = await client.workspaces.create(payload)
    return response["id"]


async def wait_for_network(
    session_or_client: aiohttp.ClientSession | ResearchCloudClient,
    network_id: str,
    timeout: float = 300,
    poll_interval: float = 5,
) -> dict:
    return await _coerce_client(session_or_client).workspaces.wait_for_network(
        network_id,
        timeout=timeout,
        poll_interval=poll_interval,
    )


async def wait_for_workspace(
    session_or_client: aiohttp.ClientSession | ResearchCloudClient,
    workspace_id: str,
    timeout: float = WORKSPACE_CREATE_TIMEOUT_SECONDS,
    poll_interval: float = WORKSPACE_CREATE_POLL_INTERVAL_SECONDS,
) -> dict:
    return await _coerce_client(session_or_client).workspaces.wait_until_ready(
        workspace_id,
        timeout=timeout,
        poll_interval=poll_interval,
    )
