from __future__ import annotations

import logging
from urllib.parse import urljoin

import aiohttp

from researchcloud.config import ResearchCloudConfig
from researchcloud.errors import ApiError, TransportError
from researchcloud.services import CatalogService, UsersService, WalletsService, WorkspacesService


logger = logging.getLogger(__name__)


class ResearchCloudClient:
    def __init__(
        self,
        *,
        config: ResearchCloudConfig | None = None,
        token: str | None = None,
        session: aiohttp.ClientSession | object | None = None,
    ):
        self.config = config or ResearchCloudConfig(token=token)
        self._session = session
        self._owns_session = False
        self.catalog = CatalogService(self)
        self.users = UsersService(self)
        self.wallets = WalletsService(self)
        self.workspaces = WorkspacesService(self)

    @classmethod
    def from_env(cls, *, session: aiohttp.ClientSession | object | None = None) -> ResearchCloudClient:
        return cls(config=ResearchCloudConfig.from_env(), session=session)

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

    async def _ensure_session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(headers=self.config.headers())
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
        url = urljoin(self.config.base_url_for(service), path)
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
