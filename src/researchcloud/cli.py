from __future__ import annotations

import argparse
import asyncio
import json
import random
import string
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from researchcloud.api import (
    DEFAULT_CLOUD_NAME,
    create_network,
    get_expected_optional_parameter_keys,
    resolve_catalog_item,
    resolve_co,
    resolve_network_and_offering,
    resolve_offering_and_flavours,
    resolve_wallet,
    to_network_cloud_name,
)
from researchcloud.client import ResearchCloudClient
from researchcloud.config import ResearchCloudConfig
from researchcloud.utils.flavours import match_size_flavour
from researchcloud.builders import build_create_payload


DEFAULT_WORKSPACE_ENDTIME = timedelta(days=3)
DEFAULT_HOST_NAME_PREFIX = "ws"


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
    content = path.read_text(encoding="utf-8")
    if suffix == ".json":
        parsed = json.loads(content)
    elif suffix in {".yaml", ".yml"}:
        parsed = yaml.safe_load(content)
    else:
        raise ValueError(f"Unsupported optional parameter file extension {suffix!r}. Use .json, .yaml or .yml.")
    return _normalize_parameter_map(parsed, source)


def parse_optional_parameters(
    cli_parameters: list[str] | None = None,
    json_blob: str | None = None,
    file_path: str | None = None,
) -> dict[str, str]:
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
            raise ValueError(f"Invalid --optional-parameter value {parameter!r}. Expected format: key=value")
        key, value = parameter.split("=", 1)
        if not key.strip():
            raise ValueError(f"Invalid --optional-parameter value {parameter!r}: key cannot be empty.")
        merged[key] = value
    return merged


def validate_config(required: list[str] | None = None) -> None:
    config = ResearchCloudConfig.from_env()
    available = {"RESEARCH_CLOUD_TOKEN": config.token}
    keys = required or list(available.keys())
    missing = [key for key in keys if not available.get(key)]
    if missing:
        print("ERROR: The following required variables are not set:")
        for name in missing:
            print(f"  {name}")
        print("\nSet them in your .env file or as environment variables.")
        sys.exit(1)


def validate_workspace_end_time(end_time: str) -> None:
    normalized = end_time.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            "--end-time must be a valid ISO 8601 datetime (for example 2026-12-31T23:59:59Z)."
        ) from exc

    if parsed.tzinfo is None:
        raise ValueError("--end-time must include a timezone (use trailing 'Z' for UTC).")

    now_utc = datetime.now(timezone.utc)
    if parsed <= now_utc:
        raise ValueError(
            f"--end-time must be in the future. Current UTC time is "
            f"{now_utc.isoformat().replace('+00:00', 'Z')}."
        )


def resolve_workspace_end_time(end_time: str | None) -> str:
    if end_time and end_time.strip():
        return end_time.strip()
    return (datetime.now(timezone.utc) + DEFAULT_WORKSPACE_ENDTIME).strftime("%Y-%m-%dT%H:%M:%SZ")


async def list_networks_for_co(
    co_name: str,
    cloud_name: str = DEFAULT_CLOUD_NAME,
    by_owner: bool = False,
    dry_run: bool = False,
) -> None:
    async with ResearchCloudClient.from_env() as client:
        co = await resolve_co(client, co_name)
        if dry_run:
            print(f"Dry run: would list private networks for CO {co['co_name']!r}  (id: {co['id']})")
            print(
                f"  params: {{'co_id': {co['id']}, 'application_type': 'Network', "
                f"'deleted': 'false', 'by_owner': {str(by_owner).lower()}}}"
            )
            print(f"  cloud filter: {cloud_name!r}")
            print(f"  network cloud filter: {to_network_cloud_name(cloud_name)!r}")
            return
        networks = await client.workspaces.list_networks(co["id"], cloud_name=cloud_name, by_owner=by_owner)
        pretty(networks)


async def create_network_for_co(
    co_name: str,
    wallet_name: str,
    cloud_name: str = DEFAULT_CLOUD_NAME,
    network_name: str | None = None,
    network_name_hint: str | None = None,
    host_name_base: str = "ws",
    dry_run: bool = False,
) -> None:
    async with ResearchCloudClient.from_env() as client:
        co, wallet = await asyncio.gather(resolve_co(client, co_name), resolve_wallet(client, wallet_name))
        if dry_run:
            print(f"Dry run: would create private network in CO {co['co_name']!r} using wallet {wallet['name']!r}")
            print(f"  network_name: {network_name or f'{host_name_base}-network-{_random_suffix()}'}")
            print(f"  cloud_name: {cloud_name}")
            print(f"  network_cloud_name: {to_network_cloud_name(cloud_name)}")
            return

        selected_network_name = network_name or f"{host_name_base}-network-{_random_suffix()}"
        products = wallet["budgets"][0]["products"]
        network_id = await create_network(
            client,
            co,
            wallet,
            products,
            cloud_name,
            selected_network_name,
            network_name_hint=network_name_hint,
        )
        print(f"Created private network {selected_network_name!r} (id: {network_id})")
        network = await client.workspaces.wait_for_network(network_id)
        print(f"Private network available: {network['id']}")
        pretty(network)


async def list_workspaces_for_co(
    co_name: str,
    cloud_name: str = DEFAULT_CLOUD_NAME,
    by_owner: bool = False,
    catalog_item_name: str | None = None,
    workspace_name: str | None = None,
    dry_run: bool = False,
) -> None:
    async with ResearchCloudClient.from_env() as client:
        co = await resolve_co(client, co_name)
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

        workspaces = await client.workspaces.list(
            co_id=co["id"],
            catalog_item_name=catalog_item_name or "",
            by_owner=by_owner,
            application_type="Compute",
            workspace_name=workspace_name,
        )
        workspaces = [
            workspace
            for workspace in workspaces
            if workspace.get("meta", {}).get("subscription_name") == cloud_name
        ]
        pretty(workspaces)


async def list_application_offerings_for_co(
    co_name: str,
    wallet_name: str,
    cloud_name: str = DEFAULT_CLOUD_NAME,
    application_type: str | None = None,
    name: str | None = None,
    dry_run: bool = False,
) -> None:
    async with ResearchCloudClient.from_env() as client:
        co, wallet = await asyncio.gather(resolve_co(client, co_name), resolve_wallet(client, wallet_name))
        products = wallet["budgets"][0]["products"]
        params = {"co": co["id"], "product": products}
        if application_type:
            params["type"] = application_type
        if dry_run:
            print(f"Dry run: would list application offerings for CO {co['co_name']!r} and wallet {wallet['name']!r}")
            print(f"  params: {params}")
            print(f"  cloud filter: {cloud_name!r}")
            return

        items = await client.catalog.list_items_with_offerings(
            co_id=co["id"],
            products=products,
            name=name,
            application_type=application_type,
        )
        filtered_items = []
        for item in items:
            offerings = await client.catalog.list_offerings_for_item(item["id"], co["id"], products)
            if any(offering["subscription"]["name"] == cloud_name for offering in offerings):
                filtered_items.append(item)
        pretty(filtered_items)


async def delete_workspace_by_id(workspace_id: str, dry_run: bool = False) -> None:
    async with ResearchCloudClient.from_env() as client:
        if dry_run:
            print(f"Dry run: would delete workspace {workspace_id!r}")
            print(f"  path: workspaces/{workspace_id}/")
            return
        await client.workspaces.delete(workspace_id)
        print(f"Deleted workspace {workspace_id!r}")


async def create_workspace(
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
    network_name_hint: str | None = None,
    storage_ids: list[str] | None = None,
    network_ids: list[str] | None = None,
    ip_ids: list[str] | None = None,
    dataset_names: list[str] | None = None,
    dataset_ids: list[str] | None = None,
    dry_run: bool = False,
    use_private_network: bool = False,
    optional_parameters: dict[str, str] | None = None,
) -> None:
    size_selection_args = sum(value is not None for value in (size_flavour_name, num_cpu, num_gpu))
    if size_selection_args > 1:
        raise ValueError("Provide at most one of size_flavour_name, num_cpu, or num_gpu.")
    if size_selection_args == 0:
        raise ValueError("Provide one of size_flavour_name, num_cpu, or num_gpu.")
    validate_config()

    normalized_host_name = host_name.strip() if host_name else ""
    selected_host_name = normalized_host_name or f"{DEFAULT_HOST_NAME_PREFIX}_{_random_suffix()}"
    selected_workspace_end_time = resolve_workspace_end_time(end_time)
    validate_workspace_end_time(selected_workspace_end_time)

    async with ResearchCloudClient.from_env() as client:
        print("\n── Resolving resources ─────────────────────────────────────")
        co, wallet = await asyncio.gather(resolve_co(client, co_name), resolve_wallet(client, wallet_name))
        products = wallet["budgets"][0]["products"]
        print(f"  CO          : {co['co_name']}  (id: {co['id']})")
        print(f"  Wallet      : {wallet['name']}  (id: {wallet['id']})")
        print(f"  Products    : {products}")

        catalog_item = await resolve_catalog_item(client, catalog_item_name, co["id"], products)
        print(f"  Catalog item: {catalog_item['name']}  (id: {catalog_item['id']})")

        resolved_size_name = None if (num_cpu is not None or num_gpu is not None) else size_flavour_name
        offering, size_flavour, os_flavour = await resolve_offering_and_flavours(
            client,
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
        print(f"  Cloud       : {offering['subscription']['name']}")
        print(f"  OS flavour  : {os_flavour['name']}")
        print(f"  Size flavour: {size_flavour['name']}")
        print(f"  Host name   : {selected_host_name}")
        if optional_parameters:
            print(f"  Optional parameters supplied: {sorted(optional_parameters.keys())}")

        attached_network_ids: list[str | dict] = list(network_ids or [])
        if use_private_network:
            print("  Private network: looking for an existing one …")
            existing_networks = await client.workspaces.list_networks(co["id"], cloud_name=cloud_name)
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
                network_name = f"{DEFAULT_HOST_NAME_PREFIX}-network-{_random_suffix()}"
                print(f"  Private network: none found — creating {network_name!r} …")
                network_id = await create_network(
                    client,
                    co,
                    wallet,
                    products,
                    cloud_name,
                    network_name,
                    network_name_hint=network_name_hint,
                )
                print(f"  Private network: created (id: {network_id}), waiting until available …")
                network = await client.workspaces.wait_for_network(network_id)
                network_ref = {
                    "id": network_id,
                    "name": network.get("name") or network_name,
                    "type": network.get("type") or "network",
                }
                print("  Private network: available.")
            attached_network_ids = [network_ref]

        print("────────────────────────────────────────────────────────────\n")
        payload = build_create_payload(
            co=co,
            wallet=wallet,
            catalog_item=catalog_item,
            offering=offering,
            os_flavour=os_flavour,
            size_flavour=size_flavour,
            workspace_name=workspace_name,
            workspace_description=description,
            end_time=selected_workspace_end_time,
            host_name=selected_host_name,
            storage_ids=storage_ids or [],
            network_ids=attached_network_ids,
            ip_ids=ip_ids or [],
            dataset_names=dataset_names or [],
            dataset_ids=dataset_ids or [],
            optional_parameters=optional_parameters,
        )

        if optional_parameters:
            expected_optional_parameter_keys = get_expected_optional_parameter_keys(offering)
            if expected_optional_parameter_keys:
                unexpected = sorted(
                    key for key in optional_parameters if key not in expected_optional_parameter_keys
                )
                if unexpected:
                    raise ValueError(
                        "Unsupported optional parameter keys for the selected application offering: "
                        f"{unexpected}. Expected keys: {sorted(expected_optional_parameter_keys)}"
                    )

        if dry_run:
            print("── Dry run — payload that would be sent ────────────────────")
            pretty(payload)
            print("────────────────────────────────────────────────────────────")
            return

        print(f"Creating workspace {workspace_name!r} …")
        response = await client.workspaces.create(payload)
        workspace_id = response.get("id")
        print("\n✓ Workspace create request accepted (HTTP 201)")
        if not workspace_id:
            print("  Workspace ID missing in API response; cannot poll final status.")
            pretty(response)
            return

        print(f"  Workspace ID : {workspace_id}")
        print("  Waiting for workspace provisioning to finish …")
        try:
            final_workspace = await client.workspaces.wait_until_ready(
                workspace_id,
                status_callback=lambda status, elapsed: print(
                    f"  Workspace status: {str(status).replace('-', ' ')}  ({int(elapsed)}s elapsed)"
                ),
            )
        except (RuntimeError, TimeoutError) as exc:
            print(f"\n✗ Workspace was created but did not become ready: {exc}")
            print("  Use 'get-workspaces --co <co-name> --name <workspace-name>' to inspect current state.")
            sys.exit(1)

    print("\n✓ Workspace is ready")
    pretty(final_workspace)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or inspect SURF Research Cloud resources.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request details and exit without making a mutating request.",
    )
    subparsers = parser.add_subparsers(dest="command")

    create_workspace_parser = subparsers.add_parser("create-workspace", help="Create a workspace.")
    create_workspace_parser.add_argument("--co", required=True, help="CO name.")
    create_workspace_parser.add_argument("--wallet", required=True, help="Wallet name.")
    create_workspace_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME, help="Cloud subscription name.")
    create_workspace_parser.add_argument("--name", required=True, help="Workspace name.")
    create_workspace_parser.add_argument("--os", dest="os_flavour_name", required=True, help="OS flavour name.")
    create_workspace_parser.add_argument("--description", default="", help="Workspace description.")
    create_workspace_parser.add_argument(
        "--catalog-item-name",
        required=True,
        help="Catalog item (application offering) name.",
    )
    create_workspace_parser.add_argument(
        "--end-time",
        help="Workspace end time in ISO 8601 format (for example 2026-12-31T23:59:59Z).",
    )
    create_workspace_parser.add_argument(
        "--host-name",
        help="Host name for the workspace. Defaults to a generated value: ws_<random-suffix>.",
    )
    create_workspace_parser.add_argument(
        "--network-name-hint",
        help="Private-network catalog item name hint (optional).",
    )
    create_workspace_parser.add_argument("--storage-id", action="append", default=[], help="Attach storage by ID.")
    create_workspace_parser.add_argument("--network-id", action="append", default=[], help="Attach network by ID.")
    create_workspace_parser.add_argument("--ip-id", action="append", default=[], help="Attach IP by ID.")
    create_workspace_parser.add_argument("--dataset-name", action="append", default=[], help="Attach dataset by name.")
    create_workspace_parser.add_argument("--dataset-id", action="append", default=[], help="Attach dataset by ID.")
    size_group = create_workspace_parser.add_mutually_exclusive_group()
    size_group.required = True
    size_group.add_argument("--size-flavour", "--size", dest="size_flavour_name")
    size_group.add_argument("--num-cpu", type=int)
    size_group.add_argument("--num-gpu", type=int)
    create_workspace_parser.add_argument("--gpu-type")
    create_workspace_parser.add_argument("--private-network", action="store_true")
    create_workspace_parser.add_argument("--optional-parameter", action="append", default=[])
    create_workspace_parser.add_argument("--optional-parameters-json")
    create_workspace_parser.add_argument("--optional-parameters-file")
    create_workspace_parser.add_argument("--dry-run", action="store_true")

    get_networks_parser = subparsers.add_parser("get-networks", help="List private networks in a CO.")
    get_networks_parser.add_argument("--co", required=True)
    get_networks_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME)
    get_networks_parser.add_argument("--by-owner", action="store_true")
    get_networks_parser.add_argument("--dry-run", action="store_true")

    create_network_parser = subparsers.add_parser("create-network", help="Create a private network in a CO.")
    create_network_parser.add_argument("--co", required=True)
    create_network_parser.add_argument("--wallet", required=True)
    create_network_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME)
    create_network_parser.add_argument("--name")
    create_network_parser.add_argument("--network-name-hint")
    create_network_parser.add_argument("--host-name-base", default="ws")
    create_network_parser.add_argument("--dry-run", action="store_true")

    delete_workspace_parser = subparsers.add_parser("delete-workspace", help="Delete a workspace by ID.")
    delete_workspace_parser.add_argument("--id", required=True)
    delete_workspace_parser.add_argument("--dry-run", action="store_true")

    get_workspaces_parser = subparsers.add_parser("get-workspaces", help="List workspaces in a CO.")
    get_workspaces_parser.add_argument("--co", required=True)
    get_workspaces_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME)
    get_workspaces_parser.add_argument("--by-owner", action="store_true")
    get_workspaces_parser.add_argument("--catalog-item-name")
    get_workspaces_parser.add_argument("--name")
    get_workspaces_parser.add_argument("--dry-run", action="store_true")

    get_offerings_parser = subparsers.add_parser(
        "get-application-offerings",
        help="List application offerings available to a CO.",
    )
    get_offerings_parser.add_argument("--co", required=True)
    get_offerings_parser.add_argument("--wallet", required=True)
    get_offerings_parser.add_argument("--cloud", default=DEFAULT_CLOUD_NAME)
    get_offerings_parser.add_argument("--type", dest="application_type")
    get_offerings_parser.add_argument("--name")
    get_offerings_parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    validate_config()

    if args.command == "get-networks":
        asyncio.run(list_networks_for_co(args.co, args.cloud, args.by_owner, args.dry_run))
    elif args.command == "create-network":
        asyncio.run(
            create_network_for_co(
                args.co,
                args.wallet,
                args.cloud,
                args.name,
                args.network_name_hint,
                args.host_name_base,
                args.dry_run,
            )
        )
    elif args.command == "get-workspaces":
        asyncio.run(
            list_workspaces_for_co(
                args.co,
                args.cloud,
                args.by_owner,
                args.catalog_item_name,
                args.name,
                args.dry_run,
            )
        )
    elif args.command == "delete-workspace":
        asyncio.run(delete_workspace_by_id(args.id, args.dry_run))
    elif args.command == "get-application-offerings":
        asyncio.run(
            list_application_offerings_for_co(
                args.co,
                args.wallet,
                args.cloud,
                args.application_type,
                args.name,
                args.dry_run,
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
                end_time=args.end_time,
                host_name=args.host_name,
                network_name_hint=args.network_name_hint,
                storage_ids=args.storage_id,
                network_ids=args.network_id,
                ip_ids=args.ip_id,
                dataset_names=args.dataset_name,
                dataset_ids=args.dataset_id,
                dry_run=args.dry_run,
                use_private_network=args.private_network,
                optional_parameters=optional_parameters,
            )
        )
    else:
        raise RuntimeError(f"Unknown command: {args.command!r}. Use --help for usage information.")
