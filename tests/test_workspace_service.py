from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from researchcloud.services.workspaces import WorkspaceCreationPlan, WorkspacesService
from researchcloud.utils.naming import generate_host_name, generate_resource_name
from researchcloud.utils.flavours import validate_size_flavour_selection


def _run(coro):
    return asyncio.run(coro)


OS_FLAVOUR = {"name": "Ubuntu 22.04", "category": "os"}
SIZE_FLAVOUR = {"name": "8 Core - 32 GB", "category": "size"}
GPU_SIZE_FLAVOURS = [
    {"name": "GPU 16 Core - 64 GB - 1x A10", "category": "size"},
    {"name": "GPU 48 Core - 192 GB - 4x A10", "category": "size"},
]
OFFERING = {
    "id": "offering-1",
    "subscription": {
        "name": "SURF HPC Cloud",
        "tag": "tag-1",
        "subscription_group": {"id": "sub-group-1"},
    },
    "application": {"name": "Compute"},
    "flavours": [OS_FLAVOUR, SIZE_FLAVOUR, *GPU_SIZE_FLAVOURS],
    "optional_parameters": {"username": {}},
}
CO = {"id": "co-1", "co_name": "Example CO"}
WALLET = {"id": "wallet-1", "name": "Example Wallet", "budgets": [{"products": ["product-1"]}]}
CATALOG_ITEM = {"id": "catalog-1", "name": "My App", "icon": ""}


def _make_client(offering: dict = OFFERING) -> MagicMock:
    client = MagicMock()
    client.resolve_co = AsyncMock(return_value=CO)
    client.resolve_wallet = AsyncMock(return_value=WALLET)
    client.resolve_catalog_item = AsyncMock(return_value=CATALOG_ITEM)
    client.resolve_offering_and_flavours = AsyncMock(return_value=(offering, SIZE_FLAVOUR, OS_FLAVOUR))
    client.validate_optional_parameters = MagicMock()
    return client


class TestBuildCreatePayloadFromNames:
    def test_resolves_payload_using_named_size_flavour(self):
        client = _make_client()
        service = WorkspacesService(client)

        plan = _run(
            service.build_create_payload_from_names(
                co_name="Example CO",
                wallet_name="Example Wallet",
                cloud_name="SURF HPC Cloud",
                catalog_item_name="My App",
                workspace_name="my-workspace",
                os_flavour_name="Ubuntu 22.04",
                size_flavour_name="8 Core - 32 GB",
                host_name="ws-fixed",
                end_time="2099-01-01T00:00:00Z",
            )
        )

        assert isinstance(plan, WorkspaceCreationPlan)
        assert plan.host_name == "ws-fixed"
        assert plan.end_time == "2099-01-01T00:00:00Z"
        assert plan.size_flavour == SIZE_FLAVOUR
        assert plan.network_ref is None
        assert plan.payload["name"] == "my-workspace"
        assert plan.payload["meta"]["flavours"] == [OS_FLAVOUR, SIZE_FLAVOUR]
        client.resolve_offering_and_flavours.assert_awaited_once_with(
            CATALOG_ITEM, "co-1", ["product-1"], "SURF HPC Cloud", "Ubuntu 22.04", "8 Core - 32 GB"
        )

    def test_resolves_size_flavour_from_num_gpu(self):
        client = _make_client()
        service = WorkspacesService(client)

        plan = _run(
            service.build_create_payload_from_names(
                co_name="Example CO",
                wallet_name="Example Wallet",
                cloud_name="SURF HPC Cloud",
                catalog_item_name="My App",
                workspace_name="my-workspace",
                os_flavour_name="Ubuntu 22.04",
                num_gpu=4,
                gpu_type="A10",
            )
        )

        assert plan.size_flavour["name"] == "GPU 48 Core - 192 GB - 4x A10"
        # size_flavour_name passed to the client resolution call must be None so the
        # offering's flavours can be fetched before matching by GPU count.
        client.resolve_offering_and_flavours.assert_awaited_once_with(
            CATALOG_ITEM, "co-1", ["product-1"], "SURF HPC Cloud", "Ubuntu 22.04", None
        )

    def test_generates_host_name_when_not_supplied(self):
        client = _make_client()
        service = WorkspacesService(client)

        plan = _run(
            service.build_create_payload_from_names(
                co_name="Example CO",
                wallet_name="Example Wallet",
                cloud_name="SURF HPC Cloud",
                catalog_item_name="My App",
                workspace_name="my-workspace",
                os_flavour_name="Ubuntu 22.04",
                size_flavour_name="8 Core - 32 GB",
                host_name_prefix="ws",
            )
        )

        assert plan.host_name.startswith("ws_")
        assert len(plan.host_name) == len("ws_") + 5

    def test_rejects_ambiguous_size_selection(self):
        client = _make_client()
        service = WorkspacesService(client)

        with pytest.raises(ValueError, match="at most one"):
            _run(
                service.build_create_payload_from_names(
                    co_name="Example CO",
                    wallet_name="Example Wallet",
                    cloud_name="SURF HPC Cloud",
                    catalog_item_name="My App",
                    workspace_name="my-workspace",
                    os_flavour_name="Ubuntu 22.04",
                    size_flavour_name="8 Core - 32 GB",
                    num_cpu=8,
                )
            )
        client.resolve_co.assert_not_awaited()

    def test_validates_optional_parameters_against_offering(self):
        client = _make_client()
        client.validate_optional_parameters.side_effect = ValueError("Unsupported optional parameter keys")
        service = WorkspacesService(client)

        with pytest.raises(ValueError, match="Unsupported optional parameter keys"):
            _run(
                service.build_create_payload_from_names(
                    co_name="Example CO",
                    wallet_name="Example Wallet",
                    cloud_name="SURF HPC Cloud",
                    catalog_item_name="My App",
                    workspace_name="my-workspace",
                    os_flavour_name="Ubuntu 22.04",
                    size_flavour_name="8 Core - 32 GB",
                    optional_parameters={"unexpected": "value"},
                )
            )

    def test_use_private_network_reuses_existing_network(self):
        client = _make_client()
        service = WorkspacesService(client)
        service.list_networks = AsyncMock(
            return_value=[{"id": "net-1", "name": "existing-net", "type": "network"}]
        )
        service.create_network = AsyncMock()

        plan = _run(
            service.build_create_payload_from_names(
                co_name="Example CO",
                wallet_name="Example Wallet",
                cloud_name="SURF HPC Cloud",
                catalog_item_name="My App",
                workspace_name="my-workspace",
                os_flavour_name="Ubuntu 22.04",
                size_flavour_name="8 Core - 32 GB",
                use_private_network=True,
            )
        )

        assert plan.network_ref == {"id": "net-1", "name": "existing-net", "type": "network"}
        assert plan.payload["meta"]["networks"] == [
            {"id": "net-1", "name": "existing-net", "type": "network"}
        ]
        service.create_network.assert_not_awaited()

    def test_use_private_network_creates_when_none_found(self):
        client = _make_client()
        service = WorkspacesService(client)
        service.list_networks = AsyncMock(return_value=[])
        service.create_network = AsyncMock(return_value="net-new")
        service.wait_for_network = AsyncMock(return_value={"id": "net-new", "name": "created-net"})

        plan = _run(
            service.build_create_payload_from_names(
                co_name="Example CO",
                wallet_name="Example Wallet",
                cloud_name="SURF HPC Cloud",
                catalog_item_name="My App",
                workspace_name="my-workspace",
                os_flavour_name="Ubuntu 22.04",
                size_flavour_name="8 Core - 32 GB",
                use_private_network=True,
            )
        )

        assert plan.network_ref == {"id": "net-new", "name": "created-net", "type": "network"}
        service.create_network.assert_awaited_once()
        service.wait_for_network.assert_awaited_once_with("net-new")

    def test_use_private_network_dry_run_skips_creation(self):
        client = _make_client()
        service = WorkspacesService(client)
        service.list_networks = AsyncMock(return_value=[])
        service.create_network = AsyncMock()

        plan = _run(
            service.build_create_payload_from_names(
                co_name="Example CO",
                wallet_name="Example Wallet",
                cloud_name="SURF HPC Cloud",
                catalog_item_name="My App",
                workspace_name="my-workspace",
                os_flavour_name="Ubuntu 22.04",
                size_flavour_name="8 Core - 32 GB",
                use_private_network=True,
                dry_run=True,
            )
        )

        assert plan.network_ref == {"id": "<new-network-id>", "name": "<new-network-id>", "type": "network"}
        service.create_network.assert_not_awaited()

    def test_reports_progress_via_callback(self):
        client = _make_client()
        service = WorkspacesService(client)
        messages: list[str] = []

        _run(
            service.build_create_payload_from_names(
                co_name="Example CO",
                wallet_name="Example Wallet",
                cloud_name="SURF HPC Cloud",
                catalog_item_name="My App",
                workspace_name="my-workspace",
                os_flavour_name="Ubuntu 22.04",
                size_flavour_name="8 Core - 32 GB",
                on_progress=messages.append,
            )
        )

        assert any("CO" in message for message in messages)
        assert any("Catalog item" in message for message in messages)


class TestNetworkResolution:
    def test_resolve_network_and_offering_prefers_matching_cloud(self):
        client = _make_client()
        client.catalog = MagicMock()
        client.catalog.list_items_with_offerings = AsyncMock(
            return_value=[{"id": "network-1", "name": "Private Network"}]
        )
        client.catalog.list_offerings_for_item = AsyncMock(
            return_value=[
                {"subscription": {"name": "Other Cloud Network"}},
                {"subscription": {"name": "SURF HPC Cloud Network"}},
            ]
        )
        client.to_network_cloud_name = MagicMock(return_value="SURF HPC Cloud Network")
        service = WorkspacesService(client)

        network, offering = _run(
            service.resolve_network_and_offering("co-1", ["product-1"], "SURF HPC Cloud")
        )

        assert network == {"id": "network-1", "name": "Private Network"}
        assert offering == {"subscription": {"name": "SURF HPC Cloud Network"}}

    def test_create_network_builds_payload_and_creates_workspace(self):
        client = _make_client()
        service = WorkspacesService(client)
        service.resolve_network_and_offering = AsyncMock(
            return_value=(
                {"id": "network-1", "name": "Private Network"},
                {
                    "id": "offering-net-1",
                    "application": {"name": "Network"},
                    "subscription": {"tag": "tag-1", "name": "SURF HPC Cloud Network", "subscription_group": {"id": "g-1"}},
                },
            )
        )
        service.create = AsyncMock(return_value={"id": "net-created"})

        network_id = _run(
            service.create_network(CO, WALLET, ["product-1"], "SURF HPC Cloud", "my-network")
        )

        assert network_id == "net-created"
        service.create.assert_awaited_once()


class TestGenerateHostName:
    def test_normalizes_supplied_host_name(self):
        assert generate_host_name("  my-host  ", "ws") == "my-host"

    def test_generates_prefixed_host_name_when_missing(self):
        generated = generate_host_name(None, "ws")
        assert generated.startswith("ws_")
        assert len(generated) == len("ws_") + 5

    def test_generates_prefixed_host_name_when_blank(self):
        generated = generate_host_name("   ", "ws")
        assert generated.startswith("ws_")


class TestGenerateResourceName:
    def test_normalizes_supplied_name(self):
        assert generate_resource_name(" my-network ", "ws-network") == "my-network"

    def test_generates_prefixed_name_when_missing(self):
        generated = generate_resource_name(None, "ws-network")
        assert generated.startswith("ws-network-")


class TestValidateSizeFlavourSelection:
    def test_accepts_exactly_one_selector(self):
        validate_size_flavour_selection("size-name", None, None)
        validate_size_flavour_selection(None, 8, None)
        validate_size_flavour_selection(None, None, 2)

    def test_rejects_none_provided(self):
        with pytest.raises(ValueError, match="Provide one of"):
            validate_size_flavour_selection(None, None, None)

    def test_rejects_multiple_provided(self):
        with pytest.raises(ValueError, match="at most one"):
            validate_size_flavour_selection("size-name", 8, None)
