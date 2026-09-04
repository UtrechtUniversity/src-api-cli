from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from researchcloud.client import ResearchCloudClient


class WalletsService:
    def __init__(self, client: ResearchCloudClient):
        self._client = client

    async def list(self, name: str | None = None) -> list:
        wallets = await self._client.request("GET", "wallet", "wallets/")
        if name:
            return [wallet for wallet in wallets if wallet["name"] == name]
        return wallets
