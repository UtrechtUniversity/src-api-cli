from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from researchcloud.client import ResearchCloudClient


class UsersService:
    def __init__(self, client: ResearchCloudClient):
        self._client = client

    async def get_self(self) -> dict:
        return await self._client.request("GET", "user", "users/self/")

    async def list_cos(self, name: str | None = None) -> list:
        user = await self.get_self()
        cos = user.get("COs", [])
        if name:
            return [co for co in cos if co["co_name"] == name]
        return cos
