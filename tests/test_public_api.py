from __future__ import annotations

import importlib
import importlib.util


def test_package_exports_small_public_surface():
    researchcloud = importlib.import_module("researchcloud")

    assert hasattr(researchcloud, "ResearchCloudClient")
    assert hasattr(researchcloud, "DEFAULT_CLOUD_NAME")
    assert not hasattr(researchcloud, "build_create_payload")
    assert not hasattr(researchcloud, "match_size_flavour")


def test_top_level_api_module_is_removed():
    assert importlib.util.find_spec("api") is None
