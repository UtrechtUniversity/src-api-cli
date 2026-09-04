from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


DEFAULT_CATALOG_BASE_URL = "https://gw.live.surfresearchcloud.nl/v1/application-market/"
DEFAULT_USER_BASE_URL = "https://gw.live.surfresearchcloud.nl/v1/user/"
DEFAULT_WALLET_BASE_URL = "https://gw.live.surfresearchcloud.nl/v1/wallet/"
DEFAULT_WORKSPACE_BASE_URL = "https://gw.live.surfresearchcloud.nl/v1/workspace/"
DEFAULT_CLOUD_NAME = "SURF HPC Cloud"


@dataclass(frozen=True, slots=True)
class ResearchCloudConfig:
    token: str | None = None
    catalog_base_url: str = DEFAULT_CATALOG_BASE_URL
    user_base_url: str = DEFAULT_USER_BASE_URL
    wallet_base_url: str = DEFAULT_WALLET_BASE_URL
    workspace_base_url: str = DEFAULT_WORKSPACE_BASE_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ResearchCloudConfig:
        source = os.environ if env is None else env
        return cls(
            token=source.get("RESEARCH_CLOUD_TOKEN"),
            catalog_base_url=source.get("CATALOG_BASE_URL", DEFAULT_CATALOG_BASE_URL),
            user_base_url=source.get("USER_BASE_URL", DEFAULT_USER_BASE_URL),
            wallet_base_url=source.get("WALLET_BASE_URL", DEFAULT_WALLET_BASE_URL),
            workspace_base_url=source.get("WORKSPACE_BASE_URL", DEFAULT_WORKSPACE_BASE_URL),
        )

    def require_token(self) -> str:
        if not self.token:
            raise ValueError("RESEARCH_CLOUD_TOKEN is required.")
        return self.token

    def headers(self) -> dict[str, str]:
        return {
            "authorization": self.require_token(),
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
