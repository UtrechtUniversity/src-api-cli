from __future__ import annotations

import logging
import os
from collections.abc import Mapping
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
