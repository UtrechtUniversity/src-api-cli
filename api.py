"""API helpers for interacting with SURF Research Cloud."""

import logging
import asyncio
from collections.abc import Mapping, Sequence
from urllib.parse import urljoin, quote_plus

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (all values come from workspace.py / .env)
# ---------------------------------------------------------------------------
from env import (
    CATALOG_BASE_URL,
    USER_BASE_URL,
    WALLET_BASE_URL,
    WORKSPACE_BASE_URL,
)

DEFAULT_CLOUD_NAME = "SURF HPC Cloud"
WORKSPACE_CREATE_TIMEOUT_SECONDS = 1800
WORKSPACE_CREATE_POLL_INTERVAL_SECONDS = 10

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_network_cloud_name(cloud_name: str) -> str:
    """Map a base cloud name (e.g. 'SURF HPC Cloud') to its network cloud name."""
    normalized = cloud_name.strip()
    if normalized.endswith(" Network"):
        return normalized
    return f"{normalized} Network"


def get_expected_optional_parameter_keys(offering: dict) -> set[str]:
    expected = set()
    for parameter in offering.get("overridable_parameters", []):
        key = parameter.get("key")
        if isinstance(key, str) and key:
            expected.add(key)
    return expected


async def make_request(
    session: aiohttp.ClientSession,
    method: str,
    base_url: str,
    path: str = "",
    params=None,
    data=None,
):
    """Make an async HTTP request and return (status_code, response_body)."""
    url = urljoin(base_url, path)
    logger.info("%-6s %s  params=%s", method, url, params)

    try:
        async with session.request(method, url, params=params, json=data) as response:
            content_type = response.headers.get("Content-Type", "")
            body = await response.json() if "application/json" in content_type else await response.text()

            if not response.ok:
                error_body = body if isinstance(body, dict) else {"message": [body]}
                logger.error(
                    "HTTP %s for %s — %s",
                    response.status,
                    url,
                    error_body.get("message", error_body),
                )
                return response.status, error_body

            logger.info("Response: %s", response.status)
            return response.status, body

    except aiohttp.ClientError as exc:
        logger.error("Request failed for %s — %s", url, exc)
        return 500, {"message": [str(exc)]}


# ---------------------------------------------------------------------------
# Lookup helpers: resolve names → objects
# ---------------------------------------------------------------------------

async def get_wallets(session: aiohttp.ClientSession, name: str | None = None) -> list:
    """Return all wallets, or filter by name."""
    _, wallets = await make_request(session, "GET", WALLET_BASE_URL, "wallets/")
    if name:
        return [w for w in wallets if w["name"] == name]
    return wallets


async def get_user_cos(session: aiohttp.ClientSession, name: str | None = None) -> list:
    """Return all COs the authenticated user belongs to, or filter by co_name."""
    _, user = await make_request(session, "GET", USER_BASE_URL, "users/self/")
    cos = user.get("COs", [])
    if name:
        return [co for co in cos if co["co_name"] == name]
    return cos


async def get_catalog_items_with_offerings(
    session: aiohttp.ClientSession,
    co_id: str,
    products: list,
    name: str | None = None,
    application_type: str | None = "Compute",
) -> list:
    """Return catalog items that have valid offerings for the given CO and wallet products."""
    params = {"co": co_id, "product": products}
    if application_type:
        params["type"] = application_type
    _, response = await make_request(session, "GET", CATALOG_BASE_URL, "catalog_items/offerings/", params=params)
    items = response.get("results", [])
    if name:
        return [ci for ci in items if ci["name"] == name]
    return items


async def get_offerings_for_item(
    session: aiohttp.ClientSession,
    catalog_item_id: str,
    co_id: str,
    products: list,
) -> list:
    """Return all application offerings for a specific catalog item."""
    params = {"co": co_id, "product": products}
    path = f"catalog_items/{quote_plus(catalog_item_id)}/offerings/"
    _, response = await make_request(session, "GET", CATALOG_BASE_URL, path, params=params)
    return response.get("results", [])


async def get_workspaces(
    session: aiohttp.ClientSession,
    co_name: str,
    catalog_item_name: str,
    by_owner: bool = False,
    application_type: str = "Compute",
    workspace_name: str | None = None,
    status: str | Sequence[str] | None = None,
    attribute_filters: Mapping[str, object] | None = None,
) -> list:
    """
    Return existing (non-deleted) workspaces for a given CO and catalog item name.

    Args:
        session:           Active aiohttp ClientSession.
        co_name:           Name of the Collaborative Organisation to filter by.
        catalog_item_name: Application Offering name (e.g. "Ubuntu Desktop").
                           Matched against the workspace's meta.application_name.
        by_owner:          When True, return only workspaces owned by the
                           authenticated user.
        application_type:  Workspace type filter, e.g. Compute, Storage, IP, Network.
        workspace_name:    When given, only workspaces whose ``name`` matches
                           exactly are returned.
        status:            Optional workspace status filter. Can be a single status
                           string or a list/tuple of statuses such as
                           ``"running"`` or ``("running", "paused")``.
        attribute_filters: Optional nested attribute filters using dot-separated
                           paths, e.g. ``{"meta.interactive_parameters": [...]}``.

    Returns:
        List of workspace dicts, sorted newest-first by time_created.
    """
    co_matches = await get_user_cos(session, co_name)
    if not co_matches:
        raise ValueError(f"No CO found with name: {co_name!r}")
    co_id = co_matches[0]["id"]

    params: dict = {
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

    workspaces: list = []
    offset = 0
    while True:
        params["offset"] = offset
        status, response = await make_request(session, "GET", WORKSPACE_BASE_URL, "workspaces/", params=params)
        if status != 200:
            raise RuntimeError(f"Failed to list workspaces (HTTP {status}): {response}")

        page = response.get("results", [])
        workspaces.extend(page)

        if response.get("next") is None:
            break
        offset += len(page)

    if catalog_item_name:
        result = [
            ws for ws in workspaces
            if ws.get("meta", {}).get("application_name") == catalog_item_name
        ]
    else:
        result = workspaces

    if workspace_name:
        result = [ws for ws in result if ws.get("name") == workspace_name]

    if len(normalized_statuses) > 1:
        allowed_statuses = set(normalized_statuses)
        result = [ws for ws in result if ws.get("status") in allowed_statuses]

    if attribute_filters:
        result = [ws for ws in result if _matches_attribute_filters(ws, attribute_filters)]

    result.sort(key=lambda ws: ws.get("time_created", ""), reverse=True)
    return result


def _normalize_status_filter(status: str | Sequence[str] | None) -> tuple[str, ...]:
    if status is None:
        return ()
    if isinstance(status, str):
        normalized = status.strip()
        return (normalized,) if normalized else ()

    normalized_statuses: list[str] = []
    for item in status:
        if not isinstance(item, str):
            raise ValueError(f"Workspace status filters must be strings, got {type(item).__name__}.")
        normalized = item.strip()
        if normalized:
            normalized_statuses.append(normalized)
    return tuple(normalized_statuses)


def _get_nested_attribute(data: Mapping[str, object], path: str) -> object:
    current: object = data
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
    return current


def _matches_expected_value(actual: object, expected: object) -> bool:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        return all(_matches_expected_value(actual.get(key), value) for key, value in expected.items())

    if isinstance(expected, Sequence) and not isinstance(expected, (str, bytes, bytearray)):
        if not isinstance(actual, Sequence) or isinstance(actual, (str, bytes, bytearray)):
            return False
        return all(any(_matches_expected_value(candidate, value) for candidate in actual) for value in expected)

    return actual == expected


def _matches_attribute_filters(workspace: Mapping[str, object], attribute_filters: Mapping[str, object]) -> bool:
    for path, expected_value in attribute_filters.items():
        if not _matches_expected_value(_get_nested_attribute(workspace, path), expected_value):
            return False
    return True


async def get_networks(
    session: aiohttp.ClientSession,
    co_id: str,
    cloud_name: str = DEFAULT_CLOUD_NAME,
    by_owner: bool = False,
) -> list:
    """
    Return existing (non-deleted) private network workspaces for a given CO.

    A private network is itself a workspace with application_type "Network",
    so this queries the same workspaces endpoint used for Compute/Storage/IP.

    Args:
        session:  Active aiohttp ClientSession.
        co_id:    ID of the Collaborative Organisation to filter by.
        cloud_name: Base cloud subscription name used to filter network clouds.
        by_owner: When True, return only networks owned by the authenticated user.

    Returns:
        List of network workspace dicts, sorted newest-first by time_created.
    """
    params: dict = {
        "co_id": co_id,
        "application_type": "Network",
        "deleted": "false",
        "limit": 100,
    }
    if by_owner:
        params["by_owner"] = "true"

    networks: list = []
    offset = 0
    while True:
        params["offset"] = offset
        status, response = await make_request(session, "GET", WORKSPACE_BASE_URL, "workspaces/", params=params)
        if status != 200:
            raise RuntimeError(f"Failed to list networks (HTTP {status}): {response}")

        page = response.get("results", [])
        networks.extend(page)

        if response.get("next") is None:
            break
        offset += len(page)

    network_cloud_name = to_network_cloud_name(cloud_name)
    networks = [
        network
        for network in networks
        if network.get("meta", {}).get("subscription_name") == network_cloud_name
    ]
    networks.sort(key=lambda ws: ws.get("time_created", ""), reverse=True)
    return networks


async def get_workspace(session: aiohttp.ClientSession, workspace_id: str) -> dict:
    """Return a single workspace by ID."""
    status, response = await make_request(
        session,
        "GET",
        WORKSPACE_BASE_URL,
        f"workspaces/{quote_plus(workspace_id)}/",
    )
    if status != 200:
        raise RuntimeError(f"Failed to retrieve workspace {workspace_id!r} (HTTP {status}): {response}")
    return response


async def trigger_workspace_action(
    session: aiohttp.ClientSession,
    workspace_id: str,
    action_type: str,
) -> dict:
    """Trigger a documented workspace action such as pause or resume."""
    normalized_action = action_type.strip().lower()
    status, response = await make_request(
        session,
        "POST",
        WORKSPACE_BASE_URL,
        f"workspaces/{quote_plus(workspace_id)}/actions/{quote_plus(normalized_action)}/",
        data={},
    )
    if status != 200:
        raise RuntimeError(
            f"Failed to trigger workspace action {normalized_action!r} for {workspace_id!r} "
            f"(HTTP {status}): {response}"
        )
    return response


async def pause_workspace(session: aiohttp.ClientSession, workspace_id: str) -> dict:
    """Pause a workspace by ID."""
    return await trigger_workspace_action(session, workspace_id, "pause")


async def resume_workspace(session: aiohttp.ClientSession, workspace_id: str) -> dict:
    """Resume a workspace by ID."""
    return await trigger_workspace_action(session, workspace_id, "resume")


async def is_workspace_running(session: aiohttp.ClientSession, workspace_id: str) -> bool:
    """Return whether a workspace is currently in the running state."""
    workspace = await get_workspace(session, workspace_id)
    return workspace.get("status") == "running"


async def delete_workspace(session: aiohttp.ClientSession, workspace_id: str) -> None:
    """Delete a workspace by ID."""
    status, response = await make_request(
        session,
        "DELETE",
        WORKSPACE_BASE_URL,
        f"workspaces/{quote_plus(workspace_id)}/",
    )
    if status != 204:
        raise RuntimeError(f"Failed to delete workspace {workspace_id!r} (HTTP {status}): {response}")


# ---------------------------------------------------------------------------
# Resolution: names → IDs / full objects
# ---------------------------------------------------------------------------

async def resolve_wallet(session: aiohttp.ClientSession, wallet_name: str) -> dict:
    matches = await get_wallets(session, wallet_name)
    if not matches:
        raise ValueError(f"No wallet found with name: {wallet_name!r}")
    if len(matches) > 1:
        logger.warning("Multiple wallets match %r — using the first one.", wallet_name)
    return matches[0]


async def resolve_co(session: aiohttp.ClientSession, co_name: str) -> dict:
    matches = await get_user_cos(session, co_name)
    if not matches:
        raise ValueError(f"No CO found with name: {co_name!r}")
    if len(matches) > 1:
        logger.warning("Multiple COs match %r — using the first one.", co_name)
    return matches[0]


async def resolve_catalog_item(
    session: aiohttp.ClientSession,
    catalog_item_name: str,
    co_id: str,
    products: list,
) -> dict:
    matches = await get_catalog_items_with_offerings(session, co_id, products, catalog_item_name)
    if not matches:
        raise ValueError(
            f"No catalog item (Application Offering) found with name: {catalog_item_name!r}. "
            "Check CO_NAME, WALLET_NAME and CATALOG_ITEM_NAME."
        )
    if len(matches) > 1:
        logger.warning("Multiple catalog items match %r — using the first one.", catalog_item_name)
    return matches[0]


async def resolve_offering_and_flavours(
    session: aiohttp.ClientSession,
    catalog_item: dict,
    co_id: str,
    products: list,
    cloud_name: str,
    os_flavour_name: str,
    size_flavour_name: str | None,
) -> tuple[dict, dict | None, dict]:
    """
    Return (offering, size_flavour, os_flavour) for the given cloud/flavour names.

    When *size_flavour_name* is ``None`` the size flavour is not resolved and
    ``None`` is returned in its place (useful when the caller will select a
    size flavour via :func:`match_size_flavour`).
    """
    offerings = await get_offerings_for_item(session, catalog_item["id"], co_id, products)
    cloud_offerings = [o for o in offerings if o["subscription"]["name"] == cloud_name]
    if not cloud_offerings:
        available = [o["subscription"]["name"] for o in offerings]
        raise ValueError(
            f"No offering found for cloud {cloud_name!r}. "
            f"Available cloud subscriptions: {available}"
        )

    offering = cloud_offerings[0]

    flavours = offering.get("flavours", [])
    os_flavours = [f for f in flavours if f["name"] == os_flavour_name]

    if not os_flavours:
        available_os = [f["name"] for f in flavours if f.get("category") == "os"]
        raise ValueError(
            f"OS flavour {os_flavour_name!r} not found. "
            f"Available OS flavours: {available_os}"
        )

    if size_flavour_name is None:
        return offering, None, os_flavours[0]

    size_flavours = [f for f in flavours if f["name"] == size_flavour_name]
    if not size_flavours:
        available_sizes = [f["name"] for f in flavours if f.get("category") == "size"]
        raise ValueError(
            f"Size flavour {size_flavour_name!r} not found. "
            f"Available size flavours: {available_sizes}"
        )

    return offering, size_flavours[0], os_flavours[0]


import re as _re


def _parse_size_flavour(name: str) -> dict:
    """
    Parse a size flavour name into structured specs.

    Recognises two broad naming conventions used by SURF ResearchCloud:

    1. ``"GPU <cpu> Core - <mem> - <n>x <type>"``   e.g. "GPU 16 Core - 64 GB - 1x A10"
    2. ``"<type> - <n> GPU"``                        e.g. "A10 - 1 GPU"
    3. ``"<n> Core - <mem> RAM"``                    e.g. "4 Core - 32 GB RAM"
    4. ``"GPU <cpu> Core - <mem>"``  (no explicit GPU count/type)

    Returns a dict with keys:
        ``cpu``      – int or None
        ``gpu``      – int or None  (number of *GPU cards*, not GPU-cores)
        ``gpu_type`` – str or None  (e.g. "A10", "V100", "A100", "RTX2080")
    """
    name = name.strip()
    cpu = gpu = gpu_type = None

    # Pattern 1 & 4: starts with "GPU <n> Core"
    m = _re.match(r"GPU\s+(\d+)\s+Core", name, _re.IGNORECASE)
    if m:
        cpu = int(m.group(1))

    # Explicit "<n>x <TYPE>" anywhere in the name  (e.g. "1x A10", "4x V100")
    m2 = _re.search(r"(\d+)x\s+([A-Za-z0-9]+)", name)
    if m2:
        gpu = int(m2.group(1))
        gpu_type = m2.group(2).upper()

    # Pattern 2: "<TYPE> - <n> GPU"  (e.g. "A10 - 2 GPU")
    if gpu is None:
        m3 = _re.match(r"([A-Za-z0-9]+)\s*-\s*(\d+)\s+GPU", name, _re.IGNORECASE)
        if m3:
            gpu_type = m3.group(1).upper()
            gpu = int(m3.group(2))

    # Pattern 3: plain CPU-only  "<n> Core"
    if cpu is None:
        m4 = _re.match(r"(\d+)\s+[Cc]ore", name)
        if m4:
            cpu = int(m4.group(1))

    if not gpu and not cpu:
        raise ValueError(f"Could not parse CPU/GPU counts from size flavour name: {name!r}")

    return {"cpu": cpu, "gpu": gpu, "gpu_type": gpu_type}


def match_size_flavour(
    flavours: list[dict],
    num_cpu: int | None = None,
    num_gpu: int | None = None,
    gpu_type: str | None = None,
) -> dict:
    """
    Return the best-matching *size* flavour from *flavours* for the given
    resource request.

    Exactly one of *num_cpu* or *num_gpu* must be supplied.

    Rules
    -----
    * Only flavours whose ``category`` is ``"size"`` are considered.
    * When *gpu_type* is given, only flavours whose parsed GPU type matches
      (case-insensitive) are eligible.
    * The best match is the flavour with the largest value that is still
      ≤ the requested value ("round down").  If every candidate exceeds the
      requested value the smallest available one is returned instead of
      raising, and a warning is logged.

    Raises
    ------
    ``ValueError`` if *flavours* contains no eligible size flavour.
    ``ValueError`` if neither *num_cpu* nor *num_gpu* is provided, or both
    are provided.
    """
    if (num_cpu is None) == (num_gpu is None):
        raise ValueError("Exactly one of num_cpu or num_gpu must be provided.")

    size_flavours = [f for f in flavours if f.get("category") == "size"]
    if not size_flavours:
        raise ValueError("No size flavours available in this offering.")

    # Filter by gpu_type first (if requested)
    if gpu_type is not None:
        gpu_type_upper = gpu_type.upper()
        typed = [
            f for f in size_flavours
            if (_parse_size_flavour(f["name"]).get("gpu_type") or "").upper() == gpu_type_upper
        ]
        if not typed:
            available_types = sorted({
                _parse_size_flavour(f["name"]).get("gpu_type") or ""
                for f in size_flavours
            } - {""})
            raise ValueError(
                f"No size flavour found for GPU type {gpu_type!r}. "
                f"Available GPU types: {available_types}"
            )
        size_flavours = typed

    # Build (parsed_value, flavour) pairs
    key = "cpu" if num_cpu is not None else "gpu"
    requested = num_cpu if num_cpu is not None else num_gpu

    candidates: list[tuple[int, dict]] = []
    for f in size_flavours:
        parsed = _parse_size_flavour(f["name"])
        val = parsed.get(key)
        if val is not None:
            candidates.append((val, f))

    if not candidates:
        available_names = [f["name"] for f in size_flavours]
        raise ValueError(
            f"Could not parse {key!r} count from any size flavour. "
            f"Available size flavours: {available_names}"
        )

    # Round-down: largest value ≤ requested
    below = [(v, f) for v, f in candidates if v <= requested]
    if below:
        best_val, best = max(below, key=lambda x: x[0])
    else:
        # All candidates exceed the requested value — pick the smallest
        best_val, best = min(candidates, key=lambda x: x[0])
        logger.warning(
            "No size flavour with %s ≤ %d found; using closest available: %r (%s=%d).",
            key, requested, best["name"], key, best_val,
        )

    return best


async def resolve_network_and_offering(
    session: aiohttp.ClientSession,
    co_id: str,
    products: list,
    cloud_name: str,
    network_name_hint: str | None = None,
) -> tuple[dict, dict]:
    """
    Return (network, offering) for a private-network workspace offering.

    The "network" here refers to the application/offer name the workspace API
    exposes for a private network. In SURF ResearchCloud this is colloquially
    treated as the network itself, so we keep the naming simple and human-facing.

    If network_name_hint is not given, the first network offering available to
    the CO/wallet is used. If no offering matches cloud_name, the first
    available offering is used instead.
    """
    networks = await get_catalog_items_with_offerings(
        session, co_id, products, name=network_name_hint, application_type="Network"
    )
    if not networks:
        raise ValueError(
            "No network found"
            + (f" with name: {network_name_hint!r}." if network_name_hint else " for this CO/wallet.")
        )
    if len(networks) > 1:
        logger.warning("Multiple network entries found — using the first one: %r", networks[0]["name"])
    network = networks[0]

    offerings = await get_offerings_for_item(session, network["id"], co_id, products)
    if not offerings:
        raise ValueError(f"No offerings found for network {network['name']!r}.")

    network_cloud_name = to_network_cloud_name(cloud_name)
    cloud_offerings = [o for o in offerings if o["subscription"]["name"] == network_cloud_name]
    if not cloud_offerings:
        available = [o["subscription"]["name"] for o in offerings]
        logger.warning(
            "No network offering found for cloud %r (available: %s) — using the first available offering instead.",
            network_cloud_name, available,
        )
        cloud_offerings = offerings

    return network, cloud_offerings[0]


# ---------------------------------------------------------------------------
# Build request payload
# ---------------------------------------------------------------------------

def build_create_payload(
    co: dict,
    wallet: dict,
    catalog_item: dict,
    offering: dict,
    os_flavour: dict,
    size_flavour: dict,
    workspace_name: str,
    workspace_description: str,
    end_time: str,
    host_name: str,
    storage_ids: list[str | dict] | None = None,
    network_ids: list[str | dict] | None = None,
    ip_ids: list[str | dict] | None = None,
    dataset_names: list | None = None,
    dataset_ids: list | None = None,
    optional_parameters: dict[str, str] | None = None,
) -> dict:
    """Assemble the full workspace creation payload."""
    def _to_meta_ref(value: str | dict, default_type: str) -> dict[str, str]:
        if isinstance(value, str):
            if not value:
                raise ValueError(f"Invalid empty {default_type} reference.")
            return {"id": value, "name": value, "type": default_type}
        if not isinstance(value, dict):
            raise ValueError(f"Invalid {default_type} reference {value!r}; expected string id or object.")

        resource_id = value.get("id")
        if not isinstance(resource_id, str) or not resource_id:
            raise ValueError(f"Invalid {default_type} reference {value!r}; missing non-empty 'id'.")

        resource_name = value.get("name")
        if not isinstance(resource_name, str) or not resource_name:
            resource_name = resource_id

        resource_type = value.get("type")
        if not isinstance(resource_type, str) or not resource_type:
            resource_type = default_type

        return {"id": resource_id, "name": resource_name, "type": resource_type}

    meta = {
        "application_offering_id": offering["id"],
        "application_name": offering["application"]["name"],
        "application_icon": catalog_item.get("icon", ""),
        "application_type": "Compute",
        "subscription_tag": offering["subscription"]["tag"],
        "subscription_name": offering["subscription"]["name"],
        "subscription_group_id": offering["subscription"]["subscription_group"]["id"],
        "co_name": co["co_name"],
        "host_name": host_name,
        "subscription_resource_type": "VM",
        "flavours": [os_flavour, size_flavour],
        "storages": [_to_meta_ref(storage, "storage") for storage in (storage_ids or [])],
        "ips": [_to_meta_ref(ip, "ip") for ip in (ip_ids or [])],
        "networks": [_to_meta_ref(network, "network") for network in (network_ids or [])],
        "dataset_names": dataset_names or [],
        "dataset_ids": dataset_ids or [],
        "interactive_parameters": [{"key": key, "value": value} for key, value in (optional_parameters or {}).items()],
        "wallet_name": wallet["name"],
        "wallet_id": wallet["id"],
    }

    return {
        "co_id": co["id"],
        "wallet_id": wallet["id"],
        "name": workspace_name,
        "description": workspace_description,
        "end_time": end_time,
        "meta": meta,
    }


def build_create_network_payload(
    co: dict,
    wallet: dict,
    catalog_item: dict,
    offering: dict,
    network_name: str,
    network_description: str = "",
) -> dict:
    """Assemble the request payload to create a private network workspace."""
    meta = {
        "application_offering_id": offering["id"],
        "application_name": offering["application"]["name"],
        "application_icon": catalog_item.get("icon", ""),
        "application_type": "Network",
        "subscription_tag": offering["subscription"]["tag"],
        "subscription_name": offering["subscription"]["name"],
        "subscription_group_id": offering["subscription"]["subscription_group"]["id"],
        "co_name": co["co_name"],
        "host_name": network_name,
        "subscription_resource_type": "Private-Network",
        "flavours": [],
        "storages": [],
        "ips": [],
        "networks": [],
        "dataset_names": [],
        "dataset_ids": [],
        "interactive_parameters": [],
        "wallet_name": wallet["name"],
        "wallet_id": wallet["id"],
    }

    return {
        "co_id": co["id"],
        "wallet_id": wallet["id"],
        "name": network_name,
        "description": network_description,
        "meta": meta,
    }


# ---------------------------------------------------------------------------
# Network creation
# ---------------------------------------------------------------------------

async def create_network(
    session: aiohttp.ClientSession,
    co: dict,
    wallet: dict,
    products: list,
    cloud_name: str,
    network_name: str,
    network_name_hint: str | None = None,
    network_description: str = "",
) -> str:
    """
    Create a new private network workspace for the given CO/wallet.

    Returns:
        The id of the newly created network workspace.
    """
    network, offering = await resolve_network_and_offering(
        session, co["co_id"], products, cloud_name, network_name_hint
    )
    payload = build_create_network_payload(co, wallet, network, offering, network_name, network_description)

    status, response = await make_request(session, "POST", WORKSPACE_BASE_URL, "workspaces/", data=payload)
    if status != 201:
        raise RuntimeError(f"Failed to create network {network_name!r} (HTTP {status}): {response}")

    return response["id"]


async def wait_for_network(
    session: aiohttp.ClientSession,
    network_id: str,
    timeout: float = 300,
    poll_interval: float = 5,
) -> dict:
    """
    Poll a network workspace by ID until it becomes available (or fails/times out).

    Returns:
        The final workspace dict once status is "available".
    """
    failure_statuses = {"failed", "unhealthy", "deleted"}
    elapsed = 0.0

    while True:
        status, response = await make_request(
            session, "GET", WORKSPACE_BASE_URL, f"workspaces/{quote_plus(network_id)}/"
        )
        if status != 200:
            raise RuntimeError(f"Failed to poll network {network_id} (HTTP {status}): {response}")

        ws_status = response.get("status")
        logger.info("Network %s status: %s", network_id, ws_status)

        if ws_status == "available":
            return response
        if ws_status in failure_statuses:
            raise RuntimeError(f"Network {network_id} entered failure status {ws_status!r}: {response}")

        if elapsed >= timeout:
            raise TimeoutError(f"Timed out after {timeout}s waiting for network {network_id} to become available.")

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval


def _is_workspace_ready_status(status: str | None) -> bool:
    return status in {"available", "running", "in-use", "paused", "full"}


def _is_workspace_failure_status(status: str | None) -> bool:
    return status in {"failed", "unhealthy", "deleted", "deleting", "unknown", "unaccounted"}


async def wait_for_workspace(
    session: aiohttp.ClientSession,
    workspace_id: str,
    timeout: float = WORKSPACE_CREATE_TIMEOUT_SECONDS,
    poll_interval: float = WORKSPACE_CREATE_POLL_INTERVAL_SECONDS,
) -> dict:
    """
    Poll a workspace by ID until creation reaches a terminal success/failure state.

    Returns:
        The final workspace dict once it reaches a ready state.
    """
    elapsed = 0.0
    last_status: str | None = None

    while True:
        status_code, response = await make_request(
            session, "GET", WORKSPACE_BASE_URL, f"workspaces/{quote_plus(workspace_id)}/"
        )
        if status_code != 200:
            raise RuntimeError(f"Failed to poll workspace {workspace_id} (HTTP {status_code}): {response}")

        ws_status = response.get("status")
        if ws_status != last_status:
            human_status = str(ws_status).replace("-", " ")
            print(f"  Workspace status: {human_status}  ({int(elapsed)}s elapsed)")
            last_status = ws_status

        if _is_workspace_ready_status(ws_status):
            return response
        if _is_workspace_failure_status(ws_status):
            raise RuntimeError(f"Workspace {workspace_id} entered failure status {ws_status!r}: {response}")

        if elapsed >= timeout:
            raise TimeoutError(f"Timed out after {int(timeout)}s waiting for workspace {workspace_id} to become ready.")

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval




