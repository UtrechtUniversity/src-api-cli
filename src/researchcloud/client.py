from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Mapping, Sequence
from urllib.parse import urljoin

import aiohttp

from researchcloud.config import (
    DEFAULT_CATALOG_BASE_URL,
    DEFAULT_USER_BASE_URL,
    DEFAULT_WALLET_BASE_URL,
    DEFAULT_WORKSPACE_BASE_URL,
)
from researchcloud.errors import ApiError, TransportError
from researchcloud.services import CatalogService, UsersService, WalletsService, WorkspacesService


logger = logging.getLogger(__name__)

# HTTP statuses considered transient and worth retrying: 429 (rate limited) and 5xx (server errors).
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_BACKOFF_MAX_SECONDS = 8.0


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
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
    ):
        self.token = token
        self.catalog_base_url = catalog_base_url
        self.user_base_url = user_base_url
        self.wallet_base_url = wallet_base_url
        self.workspace_base_url = workspace_base_url
        self._session = session
        self._owns_session = False
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
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

    def _compute_backoff_delay(self, attempt: int, retry_after: str | None) -> float:
        """Compute the delay before the next retry, honoring a Retry-After header if present."""
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass  # Ignore unparsable (e.g. HTTP-date) Retry-After values and fall back below.
        exponential_delay = self.backoff_base_seconds * (2**attempt)
        capped_delay = min(exponential_delay, self.backoff_max_seconds)
        return capped_delay + random.uniform(0, self.backoff_base_seconds)

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

        attempt = 0
        while True:
            logger.info("%-6s %s  params=%s", method, url, params)
            try:
                async with session.request(method, url, params=params, json=data) as response:
                    content_type = response.headers.get("Content-Type", "")
                    body = (
                        await response.json() if "application/json" in content_type else await response.text()
                    )
                    if response.ok:
                        return body
                    if response.status in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                        delay = self._compute_backoff_delay(attempt, response.headers.get("Retry-After"))
                        attempt += 1
                        logger.warning(
                            "%-6s %s  transient HTTP %s, retrying in %.2fs (attempt %d/%d)",
                            method,
                            url,
                            response.status,
                            delay,
                            attempt,
                            self.max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise ApiError(response.status, url, body)
            except aiohttp.ClientError as exc:
                raise TransportError(url, exc) from exc

    async def _paginate(
        self,
        method: str,
        service: str,
        path: str = "",
        params: Mapping[str, object] | None = None,
        *,
        page_size: int = 100,
        results_key: str = "results",
        next_key: str = "next",
    ) -> list:
        """Fetch every page of a limit/offset-paginated list endpoint and return the combined results.

        Used by all list-style service methods so pagination handling (offset advancement and
        "next" detection) lives in one place rather than being duplicated per service.
        """
        base_params: dict[str, object] = dict(params or {})
        base_params.setdefault("limit", page_size)
        offset = int(base_params.get("offset", 0))

        items: list = []
        while True:
            page_params = {**base_params, "offset": offset}
            response = await self.request(method, service, path, params=page_params)
            page = response.get(results_key, [])
            items.extend(page)
            if not page or response.get(next_key) is None:
                break
            offset += len(page)
        return items

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

    def validate_optional_parameters(
        self,
        offering: Mapping[str, object],
        optional_parameters: Mapping[str, str] | None,
    ) -> None:
        """Raise ValueError if optional_parameters contains keys unsupported by the offering."""
        if not optional_parameters:
            return
        expected_keys = self.get_expected_optional_parameter_keys(offering)
        if not expected_keys:
            return
        unexpected = sorted(key for key in optional_parameters if key not in expected_keys)
        if unexpected:
            raise ValueError(
                "Unsupported optional parameter keys for the selected application offering: "
                f"{unexpected}. Expected keys: {sorted(expected_keys)}"
            )
