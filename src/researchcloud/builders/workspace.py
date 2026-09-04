from __future__ import annotations


def _to_meta_ref(value: str | dict, default_type: str) -> dict[str, str]:
    if isinstance(value, str):
        if not value:
            raise ValueError(f"Invalid empty {default_type} reference.")
        return {"id": value, "name": value, "type": default_type}
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {default_type} reference {value!r}; expected string id or object.")

    resource_id = value.get("id")
    if not isinstance(resource_id, str) or not resource_id:
        raise ValueError(f"Invalid {default_type} reference {value!r}; missing non-empty 'id'.")

    resource_name = value.get("name")
    if not isinstance(resource_name, str) or not resource_name:
        resource_name = resource_id

    resource_type = value.get("type")
    if not isinstance(resource_type, str) or not resource_type:
        resource_type = default_type

    return {"id": resource_id, "name": resource_name, "type": resource_type}


def build_create_payload(
    co: dict,
    wallet: dict,
    catalog_item: dict,
    offering: dict,
    os_flavour: dict,
    size_flavour: dict,
    workspace_name: str,
    workspace_description: str,
    end_time: str,
    host_name: str,
    storage_ids: list[str | dict] | None = None,
    network_ids: list[str | dict] | None = None,
    ip_ids: list[str | dict] | None = None,
    dataset_names: list | None = None,
    dataset_ids: list | None = None,
    optional_parameters: dict[str, str] | None = None,
) -> dict:
    meta = {
        "application_offering_id": offering["id"],
        "application_name": offering["application"]["name"],
        "application_icon": catalog_item.get("icon", ""),
        "application_type": "Compute",
        "subscription_tag": offering["subscription"]["tag"],
        "subscription_name": offering["subscription"]["name"],
        "subscription_group_id": offering["subscription"]["subscription_group"]["id"],
        "co_name": co["co_name"],
        "host_name": host_name,
        "subscription_resource_type": "VM",
        "flavours": [os_flavour, size_flavour],
        "storages": [_to_meta_ref(storage, "storage") for storage in (storage_ids or [])],
        "ips": [_to_meta_ref(ip, "ip") for ip in (ip_ids or [])],
        "networks": [_to_meta_ref(network, "network") for network in (network_ids or [])],
        "dataset_names": dataset_names or [],
        "dataset_ids": dataset_ids or [],
        "interactive_parameters": [
            {"key": key, "value": value} for key, value in (optional_parameters or {}).items()
        ],
        "wallet_name": wallet["name"],
        "wallet_id": wallet["id"],
    }
    return {
        "co_id": co["id"],
        "wallet_id": wallet["id"],
        "name": workspace_name,
        "description": workspace_description,
        "end_time": end_time,
        "meta": meta,
    }


def build_create_network_payload(
    co: dict,
    wallet: dict,
    catalog_item: dict,
    offering: dict,
    network_name: str,
    network_description: str = "",
) -> dict:
    meta = {
        "application_offering_id": offering["id"],
        "application_name": offering["application"]["name"],
        "application_icon": catalog_item.get("icon", ""),
        "application_type": "Network",
        "subscription_tag": offering["subscription"]["tag"],
        "subscription_name": offering["subscription"]["name"],
        "subscription_group_id": offering["subscription"]["subscription_group"]["id"],
        "co_name": co["co_name"],
        "host_name": network_name,
        "subscription_resource_type": "Private-Network",
        "flavours": [],
        "storages": [],
        "ips": [],
        "networks": [],
        "dataset_names": [],
        "dataset_ids": [],
        "interactive_parameters": [],
        "wallet_name": wallet["name"],
        "wallet_id": wallet["id"],
    }
    return {
        "co_id": co["id"],
        "wallet_id": wallet["id"],
        "name": network_name,
        "description": network_description,
        "meta": meta,
    }
