from __future__ import annotations

import importlib


def test_root_api_module_remains_importable():
    api = importlib.import_module("api")

    assert callable(api.build_create_payload)
    assert callable(api.match_size_flavour)


def test_package_exports_client():
    researchcloud = importlib.import_module("researchcloud")

    assert hasattr(researchcloud, "ResearchCloudClient")
