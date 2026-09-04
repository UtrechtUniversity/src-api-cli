from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from urllib.parse import urljoin

import aiohttp

from researchcloud.builders import build_create_network_payload
from researchcloud.config import (
    DEFAULT_CLOUD_NAME,
    DEFAULT_CATALOG_BASE_URL,
    DEFAULT_USER_BASE_URL,
    DEFAULT_WALLET_BASE_URL,
    DEFAULT_WORKSPACE_BASE_URL,
)
from researchcloud.errors import ApiError, TransportError
from researchcloud.services import CatalogService, UsersService, WalletsService, WorkspacesService


logger = logging.getLogger(__name__)


class ResearchCloudClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        catalog_base_url: str = DEFAULT_CATALOG_BASE_URL,
        user_base_url: str = DEFAULT_USER_BASE_URL,
        wallet_base_url: str = DEFAULT_WALLET_BASE_URL,
        workspace_base_url: str = DEFAULT_WORKSPACE_BASE_URL,
        session: aiohttp.ClientSession | object | None = None,
    ):
        self.token = token
        self.catalog_base_url = catalog_base_url
        self.user_base_url = user_base_url
        self.wallet_base_url = wallet_base_url
        self.workspace_base_url = workspace_base_url
        self._session = session
        self._owns_session = False
        self.catalog = CatalogService(self)
        self.users = UsersService(self)
        self.wallets = WalletsService(self)
        self.workspaces = WorkspacesService(self)

    @classmethod
    def from_env(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        session: aiohttp.ClientSession | object | None = None,
    ) -> ResearchCloudClient:
        source = os.environ if env is None else env
        return cls(
            token=source.get("RESEARCH_CLOUD_TOKEN"),
            catalog_base_url=source.get("CATALOG_BASE_URL", DEFAULT_CATALOG_BASE_URL),
            user_base_url=source.get("USER_BASE_URL", DEFAULT_USER_BASE_URL),
            wallet_base_url=source.get("WALLET_BASE_URL", DEFAULT_WALLET_BASE_URL),
            workspace_base_url=source.get("WORKSPACE_BASE_URL", DEFAULT_WORKSPACE_BASE_URL),
            session=session,
        )

    async def __aenter__(self) -> ResearchCloudClient:
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._owns_session = False

    def _require_token(self) -> str:
        if not self.token:
            raise ValueError("RESEARCH_CLOUD_TOKEN is required.")
        return self.token

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": self._require_token(),
            "accept": "application/json",
            "content-type": "application/json",
        }

    def base_url_for(self, service: str) -> str:
        mapping = {
            "catalog": self.catalog_base_url,
            "user": self.user_base_url,
            "wallet": self.wallet_base_url,
            "workspace": self.workspace_base_url,
        }
        try:
            return mapping[service]
        except KeyError as exc:
            raise ValueError(f"Unknown ResearchCloud service {service!r}.") from exc

    @staticmethod
    def to_network_cloud_name(cloud_name: str) -> str:
        normalized = cloud_name.strip()
        if normalized.endswith(" Network"):
            return normalized
        return f"{normalized} Network"

    @staticmethod
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

    async def _ensure_session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(headers=self._headers())
            self._owns_session = True
        return self._session

    async def request(
        self,
        method: str,
        service: str,
        path: str = "",
        params=None,
        data=None,
    ):
        session = await self._ensure_session()
        url = urljoin(self.base_url_for(service), path)
        logger.info("%-6s %s  params=%s", method, url, params)

        try:
            async with session.request(method, url, params=params, json=data) as response:
                content_type = response.headers.get("Content-Type", "")
                body = await response.json() if "application/json" in content_type else await response.text()
                if not response.ok:
                    raise ApiError(response.status, url, body)
                return body
        except aiohttp.ClientError as exc:
            raise TransportError(url, exc) from exc

    async def resolve_wallet(self, wallet_name: str) -> dict:
        matches = await self.wallets.list(wallet_name)
        if not matches:
            raise ValueError(f"No wallet found with name: {wallet_name!r}")
        if len(matches) > 1:
            logger.warning("Multiple wallets match %r — using the first one.", wallet_name)
        return matches[0]

    async def resolve_co(self, co_name: str) -> dict:
        matches = await self.users.list_cos(co_name)
        if not matches:
            raise ValueError(f"No CO found with name: {co_name!r}")
        if len(matches) > 1:
            logger.warning("Multiple COs match %r — using the first one.", co_name)
        return matches[0]

    async def resolve_catalog_item(
        self,
        catalog_item_name: str,
        co_id: str,
        products: list,
    ) -> dict:
        matches = await self.catalog.list_items_with_offerings(co_id, products, catalog_item_name)
        if not matches:
            raise ValueError(
                f"No catalog item (Application Offering) found with name: {catalog_item_name!r}. "
                "Check the provided CO, wallet product scope, and catalog item name."
            )
        if len(matches) > 1:
            logger.warning("Multiple catalog items match %r — using the first one.", catalog_item_name)
        return matches[0]

    async def resolve_offering_and_flavours(
        self,
        catalog_item: dict,
        co_id: str,
        products: list,
        cloud_name: str,
        os_flavour_name: str,
        size_flavour_name: str | None,
    ) -> tuple[dict, dict | None, dict]:
        offerings = await self.catalog.list_offerings_for_item(catalog_item["id"], co_id, products)
        cloud_offerings = [offering for offering in offerings if offering["subscription"]["name"] == cloud_name]
        if not cloud_offerings:
            available = [offering["subscription"]["name"] for offering in offerings]
            raise ValueError(
                f"No offering found for cloud {cloud_name!r}. Available cloud subscriptions: {available}"
            )

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
        self,
        co_id: str,
        products: list,
        cloud_name: str,
        network_name_hint: str | None = None,
    ) -> tuple[dict, dict]:
        networks = await self.catalog.list_items_with_offerings(
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
        offerings = await self.catalog.list_offerings_for_item(network["id"], co_id, products)
        if not offerings:
            raise ValueError(f"No offerings found for network {network['name']!r}.")

        network_cloud_name = self.to_network_cloud_name(cloud_name)
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
        response = await self.workspaces.create(payload)
        return response["id"]
