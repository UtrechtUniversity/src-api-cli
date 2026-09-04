from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote_plus

if TYPE_CHECKING:
    from researchcloud.client import ResearchCloudClient


class CatalogService:
    def __init__(self, client: ResearchCloudClient):
        self._client = client

    async def list_items_with_offerings(
        self,
        co_id: str,
        products: list,
        name: str | None = None,
        application_type: str | None = "Compute",
    ) -> list:
        params = {"co": co_id, "product": products}
        if application_type:
            params["type"] = application_type
        response = await self._client.request(
            "GET",
            "catalog",
            "catalog_items/offerings/",
            params=params,
        )
        items = response.get("results", [])
        if name:
            return [item for item in items if item["name"] == name]
        return items

    async def list_offerings_for_item(
        self,
        catalog_item_id: str,
        co_id: str,
        products: list,
    ) -> list:
        path = f"catalog_items/{quote_plus(catalog_item_id)}/offerings/"
        response = await self._client.request("GET", "catalog", path, params={"co": co_id, "product": products})
        return response.get("results", [])
