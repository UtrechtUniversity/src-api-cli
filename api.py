"""
create_workspace.py — User-friendly client to create SURF Research Cloud workspaces.

Configuration is read from environment variables (or a .env file).
See env.py for the full list of configurable variables.

Usage:
    python create_workspace.py [--dry-run]

    --dry-run   Print the resolved configuration and the request payload
                without actually creating the workspace.
"""
import sys
import json
import logging
import random
import string
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, quote_plus

import aiohttp
import yaml
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (all values come from workspace.py / .env)
# ---------------------------------------------------------------------------
from env import (
    RESEARCH_CLOUD_TOKEN,
    CATALOG_BASE_URL,
    USER_BASE_URL,
    WALLET_BASE_URL,
    WORKSPACE_BASE_URL,
    CO_NAME,
    WALLET_NAME,
    CATALOG_ITEM_NAME,
    CLOUD_NAME,
    OS_FLAVOUR_NAME,
    SIZE_FLAVOUR_NAME,
    NETWORK_NAME,
    HOST_NAME_BASE,
    WORKSPACE_NAME,
    WORKSPACE_DESCRIPTION,
    WORKSPACE_END_TIME,
    STORAGE_IDS,
    NETWORK_IDS,
    IP_IDS,
    DATASET_NAMES,
    DATASET_IDS,
)

_REQUIRED = {
    "RESEARCH_CLOUD_TOKEN": RESEARCH_CLOUD_TOKEN,
    "CO_NAME": CO_NAME,
    "WALLET_NAME": WALLET_NAME,
    "CATALOG_ITEM_NAME": CATALOG_ITEM_NAME,
    "OS_FLAVOUR_NAME": OS_FLAVOUR_NAME,
    "SIZE_FLAVOUR_NAME": SIZE_FLAVOUR_NAME,
    "WORKSPACE_NAME": WORKSPACE_NAME,
}

HEADERS = {
    "authorization": RESEARCH_CLOUD_TOKEN,
    "accept": "application/json",
    "content-type": "application/json",
}
DEFAULT_CLOUD_NAME = "SURF HPC Cloud"
DEFAULT_WORKSPACE_END_TIME_DAYS = 3
WORKSPACE_CREATE_TIMEOUT_SECONDS = 1800
WORKSPACE_CREATE_POLL_INTERVAL_SECONDS = 10

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _random_suffix(length: int = 5) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def pretty(data) -> None:
    print(json.dumps(data, indent=4))


def to_network_cloud_name(cloud_name: str) -> str:
    """Map a base cloud name (e.g. 'SURF HPC Cloud') to its network cloud name."""
    normalized = cloud_name.strip()
    if normalized.endswith(" Network"):
        return normalized
    return f"{normalized} Network"


def _normalize_parameter_map(raw_parameters: object, source_description: str) -> dict[str, str]:
    if not isinstance(raw_parameters, dict):
        raise ValueError(f"Optional parameters from {source_description} must be a JSON/YAML object with key/value pairs.")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in raw_parameters.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError(f"Optional parameter keys from {source_description} must be non-empty strings.")
        if not isinstance(raw_value, str):
            raise ValueError(
                f"Optional parameter {raw_key!r} from {source_description} must have a string value "
                "(ResearchCloud optional parameters only support strings)."
            )
        normalized[raw_key] = raw_value
    return normalized


def _parse_optional_parameters_file(file_path: str) -> dict[str, str]:
    path = Path(file_path).expanduser()
    if not path.exists():
        raise ValueError(f"Optional parameter file not found: {path}")
    if not path.is_file():
        raise ValueError(f"Optional parameter path is not a file: {path}")

    source = str(path)
    suffix = path.suffix.lower()
    with path.open("r", encoding="utf-8") as f:
        content = f.read()
    if suffix == ".json":
        parsed = json.loads(content)
    elif suffix in {".yaml", ".yml"}:
        parsed = yaml.safe_load(content)
    else:
        raise ValueError(
            f"Unsupported optional parameter file extension {suffix!r}. "
            "Use .json, .yaml or .yml."
        )
    return _normalize_parameter_map(parsed, source)


def parse_optional_parameters(
    cli_parameters: list[str] | None = None,
    json_blob: str | None = None,
    file_path: str | None = None,
) -> dict[str, str]:
    """Merge optional parameter inputs from file, JSON blob and key=value CLI flags."""
    merged: dict[str, str] = {}

    if file_path:
        merged.update(_parse_optional_parameters_file(file_path))

    if json_blob:
        try:
            parsed_json = json.loads(json_blob)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON passed via --optional-parameters-json: {exc}") from exc
        merged.update(_normalize_parameter_map(parsed_json, "--optional-parameters-json"))

    for parameter in cli_parameters or []:
        if "=" not in parameter:
            raise ValueError(
                f"Invalid --optional-parameter value {parameter!r}. "
                "Expected format: key=value"
            )
        key, value = parameter.split("=", 1)
        if not key.strip():
            raise ValueError(f"Invalid --optional-parameter value {parameter!r}: key cannot be empty.")
        merged[key] = value

    return merged


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

    result.sort(key=lambda ws: ws.get("time_created", ""), reverse=True)
    return result


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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def validate_config(required: list[str] | None = None):
    keys = required or list(_REQUIRED.keys())
    missing = [k for k in keys if not _REQUIRED.get(k)]
    if missing:
        print("ERROR: The following required variables are not set:")
        for name in missing:
            print(f"  {name}")
        print("\nSet them in your .env file or as environment variables.")
        sys.exit(1)

def validate_workspace_end_time(end_time: str) -> None:
    """Ensure workspace end_time is ISO-8601 and in the future."""
    normalized = end_time.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            "WORKSPACE_END_TIME must be a valid ISO 8601 datetime "
            "(for example 2026-12-31T23:59:59Z)."
        ) from exc

    if parsed.tzinfo is None:
        raise ValueError("WORKSPACE_END_TIME must include a timezone (use trailing 'Z' for UTC).")

    now_utc = datetime.now(timezone.utc)
    if parsed <= now_utc:
        raise ValueError(
            f"WORKSPACE_END_TIME must be in the future. Current UTC time is "
            f"{now_utc.isoformat().replace('+00:00', 'Z')}."
        )


def resolve_workspace_end_time(end_time: str | None) -> str:
    """Return provided end time, or default to 3 days in the future (UTC)."""
    if end_time and end_time.strip():
        return end_time.strip()
    return (datetime.now(timezone.utc) + timedelta(days=DEFAULT_WORKSPACE_END_TIME_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


async def list_networks_for_co(
    co_name: str | None = None,
    cloud_name: str = DEFAULT_CLOUD_NAME,
    by_owner: bool = False,
    dry_run: bool = False,
):
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        co = await resolve_co(session, co_name or CO_NAME)
        if dry_run:
            print(f"Dry run: would list private networks for CO {co['co_name']!r}  (id: {co['id']})")
            print(f"  params: {{'co_id': {co['id']}, 'application_type': 'Network', 'deleted': 'false', 'by_owner': {str(by_owner).lower()}}}")
            print(f"  cloud filter: {cloud_name!r}")
            print(f"  network cloud filter: {to_network_cloud_name(cloud_name)!r}")
            return
        networks = await get_networks(session, co["id"], cloud_name=cloud_name, by_owner=by_owner)
        pretty(networks)


async def create_network_for_co(
    co_name: str | None = None,
    wallet_name: str | None = None,
    cloud_name: str | None = None,
    network_name: str | None = None,
    dry_run: bool = False,
):
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        co, wallet = await asyncio.gather(
            resolve_co(session, co_name or CO_NAME),
            resolve_wallet(session, wallet_name or WALLET_NAME),
        )
        if dry_run:
            print(f"Dry run: would create private network in CO {co['co_name']!r} using wallet {wallet['name']!r}")
            print(f"  network_name: {network_name or f'{HOST_NAME_BASE}-network-{_random_suffix()}'}")
            print(f"  cloud_name: {cloud_name or CLOUD_NAME}")
            print(f"  network_cloud_name: {to_network_cloud_name(cloud_name or CLOUD_NAME)}")
            return

        net_name = network_name or f"{HOST_NAME_BASE}-network-{_random_suffix()}"
        products = wallet["budgets"][0]["products"]
        network_id = await create_network(
            session,
            co,
            wallet,
            products,
            cloud_name or CLOUD_NAME,
            net_name,
            network_name_hint=NETWORK_NAME,
        )
        print(f"Created private network {net_name!r} (id: {network_id})")
        network = await wait_for_network(session, network_id)
        print(f"Private network available: {network['id']}")
        pretty(network)


async def list_workspaces_for_co(
    co_name: str | None = None,
    cloud_name: str = DEFAULT_CLOUD_NAME,
    by_owner: bool = False,
    catalog_item_name: str | None = None,
    workspace_name: str | None = None,
    dry_run: bool = False,
):
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        co = await resolve_co(session, co_name or CO_NAME)
        params = {
            "co_id": co["id"],
            "application_type": "Compute",
            "deleted": "false",
            "by_owner": "true" if by_owner else "false",
            "limit": 100,
        }
        if dry_run:
            print(f"Dry run: would list workspaces for CO {co['co_name']!r}  (id: {co['id']})")
            print(f"  params: {params}")
            print(f"  catalog item filter: {catalog_item_name!r}")
            print(f"  workspace name filter: {workspace_name!r}")
            print(f"  cloud filter: {cloud_name!r}")
            return
        workspaces = await get_workspaces(
            session,
            co["co_name"],
            catalog_item_name or "",
            by_owner=by_owner,
            application_type="Compute",
            workspace_name=workspace_name,
        )
        workspaces = [ws for ws in workspaces if ws.get("meta", {}).get("subscription_name") == cloud_name]
        pretty(workspaces)


async def list_application_offerings_for_co(
    co_name: str | None = None,
    wallet_name: str | None = None,
    cloud_name: str = DEFAULT_CLOUD_NAME,
    application_type: str | None = None,
    name: str | None = None,
    dry_run: bool = False,
):
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        co, wallet = await asyncio.gather(
            resolve_co(session, co_name or CO_NAME),
            resolve_wallet(session, wallet_name or WALLET_NAME),
        )
        products = wallet["budgets"][0]["products"]
        params = {"co": co["id"], "product": products}
        if application_type:
            params["type"] = application_type
        if dry_run:
            print(f"Dry run: would list application offerings for CO {co['co_name']!r} and wallet {wallet['name']!r}")
            print(f"  params: {params}")
            print(f"  cloud filter: {cloud_name!r}")
            return
        items = await get_catalog_items_with_offerings(
            session,
            co["co_id"],
            products,
            name=name,
            application_type=application_type,
        )
        filtered_items = []
        for item in items:
            offerings = await get_offerings_for_item(session, item["id"], co["co_id"], products)
            if any(offering["subscription"]["name"] == cloud_name for offering in offerings):
                filtered_items.append(item)
        items = filtered_items
        pretty(items)


async def create_workspace(
    co_name: str | None = None,
    wallet_name: str | None = None,
    cloud_name: str | None = None,
    catalog_item_name: str | None = None,
    workspace_name: str | None = None,
    os_flavour_name: str | None = None,
    size_flavour_name: str | None = None,
    num_cpu: int | None = None,
    num_gpu: int | None = None,
    gpu_type: str | None = None,
    description: str | None = None,
    dry_run: bool = False,
    use_private_network: bool = False,
    optional_parameters: dict[str, str] | None = None,
):
    """
    Create a ResearchCloud workspace.

    Size selection
    --------------
    Pass *exactly one* of:

    * ``size_flavour_name`` – exact flavour name (existing behaviour)
    * ``num_cpu``           – desired number of CPU cores
    * ``num_gpu``           – desired number of GPU cards

    When using ``num_cpu`` / ``num_gpu``, ``gpu_type`` can further filter
    candidates (e.g. ``gpu_type="A10"``).  The best match ≤ the requested
    value is selected ("round down").

    If none of these three is supplied, ``SIZE_FLAVOUR_NAME`` from the
    environment is used as before.
    """
    size_selection_args = sum(x is not None for x in (size_flavour_name, num_cpu, num_gpu))
    if size_selection_args > 1:
        raise ValueError(
            "Provide at most one of size_flavour_name, num_cpu, or num_gpu."
        )

    required = [k for k in _REQUIRED.keys() if k not in {"CO_NAME", "WALLET_NAME", "CATALOG_ITEM_NAME", "OS_FLAVOUR_NAME", "WORKSPACE_NAME"}]
    if not co_name:
        required.append("CO_NAME")
    if not wallet_name:
        required.append("WALLET_NAME")
    if not catalog_item_name:
        required.append("CATALOG_ITEM_NAME")
    if not os_flavour_name:
        required.append("OS_FLAVOUR_NAME")
    if not workspace_name:
        required.append("WORKSPACE_NAME")
    # SIZE_FLAVOUR_NAME is only required from env when no programmatic size arg is given
    if size_selection_args == 0 and "SIZE_FLAVOUR_NAME" not in required:
        required.append("SIZE_FLAVOUR_NAME")
    validate_config(required)

    host_name = f"{HOST_NAME_BASE}_{_random_suffix()}"
    selected_cloud_name = cloud_name or CLOUD_NAME
    selected_catalog_item_name = catalog_item_name or CATALOG_ITEM_NAME
    selected_os_flavour_name = os_flavour_name or OS_FLAVOUR_NAME
    selected_workspace_description = WORKSPACE_DESCRIPTION if description is None else description
    selected_workspace_end_time = resolve_workspace_end_time(WORKSPACE_END_TIME)
    validate_workspace_end_time(selected_workspace_end_time)

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        print("\n── Resolving resources ─────────────────────────────────────")

        co, wallet = await asyncio.gather(
            resolve_co(session, co_name or CO_NAME),
            resolve_wallet(session, wallet_name or WALLET_NAME),
        )
        products = wallet["budgets"][0]["products"]
        print(f"  CO          : {co['co_name']}  (id: {co['id']})")
        print(f"  Wallet      : {wallet['name']}  (id: {wallet['id']})")
        print(f"  Products    : {products}")

        catalog_item = await resolve_catalog_item(session, selected_catalog_item_name, co["co_id"], products)
        print(f"  Catalog item: {catalog_item['name']}  (id: {catalog_item['id']})")

        # When selecting by cpu/gpu count, skip name-based size resolution
        resolved_size_name = (
            None if (num_cpu is not None or num_gpu is not None)
            else (size_flavour_name or SIZE_FLAVOUR_NAME)
        )
        offering, size_flavour, os_flavour = await resolve_offering_and_flavours(
            session,
            catalog_item,
            co["co_id"],
            products,
            selected_cloud_name,
            selected_os_flavour_name,
            resolved_size_name,
        )
        if num_cpu is not None or num_gpu is not None:
            flavours = offering.get("flavours", [])
            size_flavour = match_size_flavour(
                flavours,
                num_cpu=num_cpu,
                num_gpu=num_gpu,
                gpu_type=gpu_type,
            )
        print(f"  Cloud       : {offering['subscription']['name']}")
        print(f"  OS flavour  : {os_flavour['name']}")
        print(f"  Size flavour: {size_flavour['name']}")
        print(f"  Host name   : {host_name}")
        if optional_parameters:
            print(f"  Optional parameters supplied: {sorted(optional_parameters.keys())}")

        network_ids: list[str | dict] = list(NETWORK_IDS)
        if use_private_network:
            print("  Private network: looking for an existing one …")
            existing_networks = await get_networks(session, co["id"], cloud_name=selected_cloud_name)

            if existing_networks:
                network = existing_networks[0]
                network_id = network["id"]
                network_ref = {
                    "id": network_id,
                    "name": network.get("name") or network_id,
                    "type": network.get("type") or "network",
                }
                print(f"  Private network: reusing {network.get('name')!r}  (id: {network_id})")
            elif dry_run:
                network_id = "<new-network-id>"
                network_ref = {"id": network_id, "name": network_id, "type": "network"}
                print("  Private network: none found — a new one would be created (skipped for --dry-run).")
            else:
                network_name = f"{HOST_NAME_BASE}-network-{_random_suffix()}"
                print(f"  Private network: none found — creating {network_name!r} …")
                network_id = await create_network(
                    session, co, wallet, products, selected_cloud_name, network_name,
                    network_name_hint=NETWORK_NAME,
                )
                print(f"  Private network: created (id: {network_id}), waiting until available …")
                network = await wait_for_network(session, network_id)
                network_ref = {
                    "id": network_id,
                    "name": network.get("name") or network_name,
                    "type": network.get("type") or "network",
                }
                print("  Private network: available.")

            network_ids = [network_ref]

        print("────────────────────────────────────────────────────────────\n")

        payload = build_create_payload(
            co=co,
            wallet=wallet,
            catalog_item=catalog_item,
            offering=offering,
            os_flavour=os_flavour,
            size_flavour=size_flavour,
            workspace_name=workspace_name or WORKSPACE_NAME,
            workspace_description=selected_workspace_description,
            end_time=selected_workspace_end_time,
            host_name=host_name,
            storage_ids=STORAGE_IDS,
            network_ids=network_ids,
            ip_ids=IP_IDS,
            dataset_names=DATASET_NAMES,
            dataset_ids=DATASET_IDS,
            optional_parameters=optional_parameters,
        )

        if optional_parameters:
            expected_optional_parameter_keys = get_expected_optional_parameter_keys(offering)
            if expected_optional_parameter_keys:
                unexpected = sorted(key for key in optional_parameters if key not in expected_optional_parameter_keys)
                if unexpected:
                    raise ValueError(
                        "Unsupported optional parameter keys for the selected application offering: "
                        f"{unexpected}. Expected keys: {sorted(expected_optional_parameter_keys)}"
                    )
            else:
                logger.warning(
                    "Could not determine expected optional parameter keys for offering %r; "
                    "skipping key validation.",
                    offering.get("id"),
                )

        if dry_run:
            print("── Dry run — payload that would be sent ────────────────────")
            pretty(payload)
            print("────────────────────────────────────────────────────────────")
            return

        print(f"Creating workspace {workspace_name or WORKSPACE_NAME!r} …")
        status, response = await make_request(session, "POST", WORKSPACE_BASE_URL, "workspaces/", data=payload)
        if status != 201:
            print(f"\n✗ Workspace creation failed (HTTP {status})")
            pretty(response)
            sys.exit(1)

        workspace_id = response.get("id")
        print(f"\n✓ Workspace create request accepted (HTTP {status})")
        if not workspace_id:
            print("  Workspace ID missing in API response; cannot poll creation status.")
            pretty(response)
            return

        print(f"  Workspace ID : {workspace_id}")
        print("  Waiting for workspace provisioning to finish …")
        try:
            final_workspace = await wait_for_workspace(session, workspace_id)
        except (RuntimeError, TimeoutError) as exc:
            print(f"\n✗ Workspace was created but did not become ready: {exc}")
            print("  Use 'get-workspaces --name <workspace-name>' to inspect current state.")
            sys.exit(1)

    print("\n✓ Workspace is ready")
    pretty(final_workspace)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create or inspect SURF Research Cloud resources.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request details and exit without making a mutating request.",
    )
    subparsers = parser.add_subparsers(dest="command")

    create_workspace_parser = subparsers.add_parser("create-workspace", help="Create a workspace.")
    create_workspace_parser.add_argument("--co", help="CO name.")
    create_workspace_parser.add_argument("--wallet", help="Wallet name.")
    create_workspace_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME, help="Cloud subscription name.")
    create_workspace_parser.add_argument("--name", help="Workspace name override.")
    create_workspace_parser.add_argument("--os", dest="os_flavour_name", help="OS flavour name override.")
    create_workspace_parser.add_argument("--description", help="Workspace description override.")
    create_workspace_parser.add_argument("--catalog-item-name", help="Catalog item (application offering) name.")
    size_group = create_workspace_parser.add_mutually_exclusive_group()
    size_group.add_argument("--size", dest="size_flavour_name", help="Exact size flavour name.")
    size_group.add_argument("--num-cpu", type=int, help="Select size by number of CPU cores (rounds down to largest available ≤ N).")
    size_group.add_argument("--num-gpu", type=int, help="Select size by number of GPU cards (rounds down to largest available ≤ N).")
    create_workspace_parser.add_argument("--gpu-type", help="GPU model filter used with --num-gpu (e.g. A10, A100, RTX2080).")
    create_workspace_parser.add_argument("--private-network", action="store_true", help="Attach or auto-create a private network.")
    create_workspace_parser.add_argument(
        "--optional-parameter",
        action="append",
        default=[],
        help="Optional workspace parameter in key=value form. Can be passed multiple times.",
    )
    create_workspace_parser.add_argument(
        "--optional-parameters-json",
        help="Optional workspace parameters as a JSON object string, e.g. '{\"key\":\"value\"}'.",
    )
    create_workspace_parser.add_argument(
        "--optional-parameters-file",
        help="Path to a JSON or YAML file containing optional workspace parameters as key/value pairs.",
    )
    create_workspace_parser.add_argument("--dry-run", action="store_true", help="Print the payload and exit without creating the workspace.")

    get_networks_parser = subparsers.add_parser("get-networks", help="List private networks in a CO.")
    get_networks_parser.add_argument("--co", help="CO name to inspect.")
    get_networks_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME, help="Cloud subscription name filter.")
    get_networks_parser.add_argument("--by-owner", action="store_true", help="Only show networks owned by the authenticated user.")
    get_networks_parser.add_argument("--dry-run", action="store_true", help="Print the request that would be made and exit.")

    create_network_parser = subparsers.add_parser("create-network", help="Create a private network in a CO.")
    create_network_parser.add_argument("--co", help="CO name.")
    create_network_parser.add_argument("--wallet", help="Wallet name.")
    create_network_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME, help="Cloud subscription name.")
    create_network_parser.add_argument("--name", help="Private network name to create.")
    create_network_parser.add_argument("--dry-run", action="store_true", help="Print the request that would be made and exit.")

    get_workspaces_parser = subparsers.add_parser("get-workspaces", help="List workspaces in a CO.")
    get_workspaces_parser.add_argument("--co", help="CO name to inspect.")
    get_workspaces_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME, help="Cloud subscription name filter.")
    get_workspaces_parser.add_argument("--by-owner", action="store_true", help="Only show workspaces owned by the authenticated user.")
    get_workspaces_parser.add_argument("--catalog-item-name", help="Filter by catalog item (application offering) name.")
    get_workspaces_parser.add_argument("--name", help="Filter by workspace name.")
    get_workspaces_parser.add_argument("--dry-run", action="store_true", help="Print the request that would be made and exit.")

    get_offerings_parser = subparsers.add_parser("get-application-offerings", help="List application offerings available to a CO.")
    get_offerings_parser.add_argument("--co", help="CO name.")
    get_offerings_parser.add_argument("--wallet", help="Wallet name.")
    get_offerings_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME, help="Cloud subscription name filter.")
    get_offerings_parser.add_argument("--type", dest="application_type", help="Filter by application type (Compute, Storage, IP, Network).")
    get_offerings_parser.add_argument("--name", help="Filter by application name.")
    get_offerings_parser.add_argument("--dry-run", action="store_true", help="Print the request that would be made and exit.")

    args = parser.parse_args()

    if args.command == "get-networks":
        asyncio.run(list_networks_for_co(
            co_name=args.co,
            cloud_name=args.cloud,
            by_owner=args.by_owner,
            dry_run=args.dry_run,
        ))
    elif args.command == "create-network":
        asyncio.run(create_network_for_co(
            co_name=args.co,
            wallet_name=args.wallet,
            cloud_name=args.cloud,
            network_name=args.name,
            dry_run=args.dry_run,
        ))
    elif args.command == "get-workspaces":
        asyncio.run(list_workspaces_for_co(
            co_name=args.co,
            cloud_name=args.cloud,
            by_owner=args.by_owner,
            catalog_item_name=args.catalog_item_name,
            workspace_name=args.name,
            dry_run=args.dry_run,
        ))
    elif args.command == "get-application-offerings":
        asyncio.run(list_application_offerings_for_co(
            co_name=args.co,
            wallet_name=args.wallet,
            cloud_name=args.cloud,
            application_type=args.application_type,
            name=args.name,
            dry_run=args.dry_run,
        ))
    elif args.command == "create-workspace":
        optional_parameters = parse_optional_parameters(
            cli_parameters=args.optional_parameter,
            json_blob=args.optional_parameters_json,
            file_path=args.optional_parameters_file,
        )
        asyncio.run(create_workspace(
            co_name=args.co,
            wallet_name=args.wallet,
            cloud_name=args.cloud,
            catalog_item_name=args.catalog_item_name,
            workspace_name=args.name,
            os_flavour_name=args.os_flavour_name,
            size_flavour_name=args.size_flavour_name,
            num_cpu=args.num_cpu,
            num_gpu=args.num_gpu,
            gpu_type=args.gpu_type,
            description=args.description,
            dry_run=args.dry_run,
            use_private_network=args.private_network,
            optional_parameters=optional_parameters,
        ))
    else:
        raise RuntimeError(f"Unknown command: {args.command!r}. Use --help for usage information.")
