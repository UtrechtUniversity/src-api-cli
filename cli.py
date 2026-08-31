"""Command-line interface for SURF Research Cloud helpers."""

import argparse
import asyncio
import json
import random
import string
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import yaml

from api import (
    DEFAULT_CLOUD_NAME,
    build_create_payload,
    create_network,
    get_catalog_items_with_offerings,
    get_expected_optional_parameter_keys,
    get_networks,
    get_offerings_for_item,
    get_workspaces,
    logger,
    make_request,
    match_size_flavour,
    resolve_catalog_item,
    resolve_co,
    resolve_offering_and_flavours,
    resolve_wallet,
    to_network_cloud_name,
    wait_for_network,
    wait_for_workspace,
)
from env import (
    CATALOG_ITEM_NAME,
    CLOUD_NAME,
    CO_NAME,
    DATASET_IDS,
    DATASET_NAMES,
    HOST_NAME_BASE,
    IP_IDS,
    NETWORK_IDS,
    NETWORK_NAME,
    OS_FLAVOUR_NAME,
    RESEARCH_CLOUD_TOKEN,
    SIZE_FLAVOUR_NAME,
    STORAGE_IDS,
    WALLET_NAME,
    WORKSPACE_BASE_URL,
    WORKSPACE_DESCRIPTION,
    WORKSPACE_END_TIME,
    WORKSPACE_NAME,
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

DEFAULT_WORKSPACE_END_TIME_DAYS = 3


def _random_suffix(length: int = 5) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def pretty(data) -> None:
    print(json.dumps(data, indent=4))


def _normalize_parameter_map(raw_parameters: object, source_description: str) -> dict[str, str]:
    if not isinstance(raw_parameters, dict):
        raise ValueError(
            f"Optional parameters from {source_description} must be a JSON/YAML object with key/value pairs."
        )
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
            print(
                f"  params: {{'co_id': {co['id']}, 'application_type': 'Network', "
                f"'deleted': 'false', 'by_owner': {str(by_owner).lower()}}}"
            )
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
        pretty(filtered_items)


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
    """Create a ResearchCloud workspace using environment-backed defaults."""
    size_selection_args = sum(x is not None for x in (size_flavour_name, num_cpu, num_gpu))
    if size_selection_args > 1:
        raise ValueError("Provide at most one of size_flavour_name, num_cpu, or num_gpu.")

    required = [
        key for key in _REQUIRED
        if key not in {"CO_NAME", "WALLET_NAME", "CATALOG_ITEM_NAME", "OS_FLAVOUR_NAME", "WORKSPACE_NAME"}
    ]
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

        resolved_size_name = None if (num_cpu is not None or num_gpu is not None) else (size_flavour_name or SIZE_FLAVOUR_NAME)
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
            size_flavour = match_size_flavour(
                offering.get("flavours", []),
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
                    session,
                    co,
                    wallet,
                    products,
                    selected_cloud_name,
                    network_name,
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
            print("  Workspace ID missing in API response; cannot poll final status.")
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


def main() -> None:
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
    size_group.add_argument(
        "--num-cpu",
        type=int,
        help="Select size by number of CPU cores (rounds down to largest available ≤ N).",
    )
    size_group.add_argument(
        "--num-gpu",
        type=int,
        help="Select size by number of GPU cards (rounds down to largest available ≤ N).",
    )
    create_workspace_parser.add_argument(
        "--gpu-type",
        help="GPU model filter used with --num-gpu (e.g. A10, A100, RTX2080).",
    )
    create_workspace_parser.add_argument(
        "--private-network",
        action="store_true",
        help="Attach or auto-create a private network.",
    )
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
    create_workspace_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payload and exit without creating the workspace.",
    )

    get_networks_parser = subparsers.add_parser("get-networks", help="List private networks in a CO.")
    get_networks_parser.add_argument("--co", help="CO name to inspect.")
    get_networks_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME, help="Cloud subscription name filter.")
    get_networks_parser.add_argument(
        "--by-owner",
        action="store_true",
        help="Only show networks owned by the authenticated user.",
    )
    get_networks_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request that would be made and exit.",
    )

    create_network_parser = subparsers.add_parser("create-network", help="Create a private network in a CO.")
    create_network_parser.add_argument("--co", help="CO name.")
    create_network_parser.add_argument("--wallet", help="Wallet name.")
    create_network_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME, help="Cloud subscription name.")
    create_network_parser.add_argument("--name", help="Private network name to create.")
    create_network_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request that would be made and exit.",
    )

    get_workspaces_parser = subparsers.add_parser("get-workspaces", help="List workspaces in a CO.")
    get_workspaces_parser.add_argument("--co", help="CO name to inspect.")
    get_workspaces_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME, help="Cloud subscription name filter.")
    get_workspaces_parser.add_argument(
        "--by-owner",
        action="store_true",
        help="Only show workspaces owned by the authenticated user.",
    )
    get_workspaces_parser.add_argument(
        "--catalog-item-name",
        help="Filter by catalog item (application offering) name.",
    )
    get_workspaces_parser.add_argument("--name", help="Filter by workspace name.")
    get_workspaces_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request that would be made and exit.",
    )

    get_offerings_parser = subparsers.add_parser(
        "get-application-offerings",
        help="List application offerings available to a CO.",
    )
    get_offerings_parser.add_argument("--co", help="CO name.")
    get_offerings_parser.add_argument("--wallet", help="Wallet name.")
    get_offerings_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME, help="Cloud subscription name filter.")
    get_offerings_parser.add_argument(
        "--type",
        dest="application_type",
        help="Filter by application type (Compute, Storage, IP, Network).",
    )
    get_offerings_parser.add_argument("--name", help="Filter by application name.")
    get_offerings_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request that would be made and exit.",
    )

    args = parser.parse_args()

    if args.command == "get-networks":
        asyncio.run(
            list_networks_for_co(
                co_name=args.co,
                cloud_name=args.cloud,
                by_owner=args.by_owner,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "create-network":
        asyncio.run(
            create_network_for_co(
                co_name=args.co,
                wallet_name=args.wallet,
                cloud_name=args.cloud,
                network_name=args.name,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "get-workspaces":
        asyncio.run(
            list_workspaces_for_co(
                co_name=args.co,
                cloud_name=args.cloud,
                by_owner=args.by_owner,
                catalog_item_name=args.catalog_item_name,
                workspace_name=args.name,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "get-application-offerings":
        asyncio.run(
            list_application_offerings_for_co(
                co_name=args.co,
                wallet_name=args.wallet,
                cloud_name=args.cloud,
                application_type=args.application_type,
                name=args.name,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "create-workspace":
        optional_parameters = parse_optional_parameters(
            cli_parameters=args.optional_parameter,
            json_blob=args.optional_parameters_json,
            file_path=args.optional_parameters_file,
        )
        asyncio.run(
            create_workspace(
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
            )
        )
    else:
        raise RuntimeError(f"Unknown command: {args.command!r}. Use --help for usage information.")


if __name__ == "__main__":
    main()
