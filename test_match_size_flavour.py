"""Tests for payload, datetime, and size flavour helpers."""
from datetime import datetime, timedelta, timezone

import pytest
from api import (
    _is_workspace_failure_status,
    _is_workspace_ready_status,
    _parse_size_flavour,
    build_create_payload,
    match_size_flavour,
)
from cli import resolve_workspace_end_time, validate_workspace_end_time


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _size(name: str) -> dict:
    return {"name": name, "category": "size"}


def _os(name: str) -> dict:
    return {"name": name, "category": "os"}


# A representative set of flavours drawn from real ResearchCloud examples.
FLAVOURS = [
    _size("GPU 16 Core - 64 GB - 1x A10"),
    _size("GPU 48 Core - 192 GB - 4x A10"),
    _size("GPU 15 Core - 240 GB - 1x A10"),
    _size("GPU 30 Core - 480 GB - 2x A10"),
    _size("GPU 24 Core - 220 GB - 1x A100"),
    _size("GPU 48 Core - 440 GB - 2x A100"),
    _size("GPU 96 Core - 880 GB - 4x A100"),
    _size("GPU 32 Core - 244 GB - 4x V100 (disabled)"),
    _size("GPU 4 Core - 61 GB"),
    _size("A10 - 1 GPU"),
    _size("A10 - 2 GPU"),
    _size("A10 - 4 GPU (disabled)"),
    _size("RTX2080 - 1 GPU"),
    _size("RTX2080 - 2 GPU"),
    _size("1 Core - 8 GB RAM"),
    _size("2 Core- 16 GB RAM"),
    _size("4 core - 16GB RAM"),
    _size("4 Core - 32 GB RAM"),
    _size("8 Core - 32 GB RAM"),
    _size("8 Core - 64GB RAM"),
    _os("Ubuntu 22.04"),
]


# ---------------------------------------------------------------------------
# _parse_size_flavour
# ---------------------------------------------------------------------------

class TestParseSizeFlavour:
    @pytest.mark.parametrize("name,expected", [
        # Pattern 1: GPU <cpu> Core + <n>x <TYPE>
        ("GPU 16 Core - 64 GB - 1x A10",        {"cpu": 16, "gpu": 1,  "gpu_type": "A10"}),
        ("GPU 48 Core - 192 GB - 4x A10",        {"cpu": 48, "gpu": 4,  "gpu_type": "A10"}),
        ("GPU 32 Core - 244 GB - 4x V100 (disabled)", {"cpu": 32, "gpu": 4, "gpu_type": "V100"}),
        ("GPU 24 Core - 220 GB - 1x A100",       {"cpu": 24, "gpu": 1,  "gpu_type": "A100"}),
        ("GPU 96 Core - 880 GB - 4x A100",       {"cpu": 96, "gpu": 4,  "gpu_type": "A100"}),
        # Pattern 4: GPU <cpu> Core, no GPU count/type
        ("GPU 4 Core - 61 GB",                   {"cpu": 4,  "gpu": None, "gpu_type": None}),
        # Pattern 2: <TYPE> - <n> GPU
        ("A10 - 1 GPU",                          {"cpu": None, "gpu": 1, "gpu_type": "A10"}),
        ("A10 - 2 GPU",                          {"cpu": None, "gpu": 2, "gpu_type": "A10"}),
        ("RTX2080 - 1 GPU",                      {"cpu": None, "gpu": 1, "gpu_type": "RTX2080"}),
        ("RTX2080 - 2 GPU",                      {"cpu": None, "gpu": 2, "gpu_type": "RTX2080"}),
        # Pattern 3: plain CPU
        ("1 Core - 8 GB RAM",                    {"cpu": 1, "gpu": None, "gpu_type": None}),
        ("4 core - 16GB RAM",                    {"cpu": 4, "gpu": None, "gpu_type": None}),
        ("8 Core - 64GB RAM",                    {"cpu": 8, "gpu": None, "gpu_type": None}),
        # Leading/trailing whitespace is stripped
        ("  8 Core - 32 GB RAM  ",               {"cpu": 8, "gpu": None, "gpu_type": None}),
    ])
    def test_known_names(self, name, expected):
        assert _parse_size_flavour(name) == expected

    def test_gpu_type_is_uppercased(self):
        result = _parse_size_flavour("GPU 16 Core - 64 GB - 1x a10")
        assert result["gpu_type"] == "A10"

    def test_unknown_name_returns_nones(self):
        result = _parse_size_flavour("something completely unrecognised")
        assert result == {"cpu": None, "gpu": None, "gpu_type": None}


# ---------------------------------------------------------------------------
# match_size_flavour — argument validation
# ---------------------------------------------------------------------------

class TestMatchSizeFlavourValidation:
    def test_neither_cpu_nor_gpu_raises(self):
        with pytest.raises(ValueError, match="Exactly one"):
            match_size_flavour(FLAVOURS)

    def test_both_cpu_and_gpu_raises(self):
        with pytest.raises(ValueError, match="Exactly one"):
            match_size_flavour(FLAVOURS, num_cpu=4, num_gpu=1)

    def test_no_size_flavours_raises(self):
        with pytest.raises(ValueError, match="No size flavours"):
            match_size_flavour([_os("Ubuntu 22.04")], num_cpu=4)

    def test_unknown_gpu_type_raises(self):
        with pytest.raises(ValueError, match="No size flavour found for GPU type"):
            match_size_flavour(FLAVOURS, num_gpu=1, gpu_type="H100")

    def test_unparseable_key_raises(self):
        """If no size flavour exposes the requested key, raise ValueError."""
        cpu_only = [_size("4 Core - 32 GB RAM"), _size("8 Core - 64GB RAM")]
        with pytest.raises(ValueError, match="Could not parse 'gpu'"):
            match_size_flavour(cpu_only, num_gpu=1)


# ---------------------------------------------------------------------------
# match_size_flavour — CPU selection
# ---------------------------------------------------------------------------

class TestMatchSizeFlavourCpu:
    def test_exact_match(self):
        f = match_size_flavour(FLAVOURS, num_cpu=8)
        assert f["name"] in ("8 Core - 32 GB RAM", "8 Core - 64GB RAM")

    def test_round_down(self):
        # Requesting 6 cores; available CPU sizes include 4 and 8 → pick 4
        f = match_size_flavour(FLAVOURS, num_cpu=6)
        assert _parse_size_flavour(f["name"])["cpu"] == 4

    def test_large_value_picks_best_below(self):
        # 20 cores: closest below from the full list is 16 (GPU 16 Core...)
        f = match_size_flavour(FLAVOURS, num_cpu=20)
        assert _parse_size_flavour(f["name"])["cpu"] == 16

    def test_below_all_candidates_uses_smallest(self, caplog):
        # 1 core requested but lowest available is 4 → warn and return 4
        large_only = [_size("4 Core - 32 GB RAM"), _size("8 Core - 64GB RAM")]
        import logging
        with caplog.at_level(logging.WARNING, logger="api"):
            f = match_size_flavour(large_only, num_cpu=2)
        assert _parse_size_flavour(f["name"])["cpu"] == 4
        assert "No size flavour" in caplog.text

    def test_non_size_flavours_ignored(self):
        mixed = [_os("Ubuntu 22.04"), _size("4 Core - 32 GB RAM")]
        f = match_size_flavour(mixed, num_cpu=99)
        assert f["name"] == "4 Core - 32 GB RAM"


# ---------------------------------------------------------------------------
# match_size_flavour — GPU selection
# ---------------------------------------------------------------------------

class TestMatchSizeFlavourGpu:
    def test_exact_gpu_count(self):
        f = match_size_flavour(FLAVOURS, num_gpu=2, gpu_type="A10")
        assert _parse_size_flavour(f["name"])["gpu"] == 2
        assert _parse_size_flavour(f["name"])["gpu_type"] == "A10"

    def test_exact_gpu_count_no_type_filter(self):
        # Without gpu_type, any flavour with gpu ≤ requested qualifies
        f = match_size_flavour(FLAVOURS, num_gpu=1)
        assert _parse_size_flavour(f["name"])["gpu"] == 1

    def test_round_down_gpu(self):
        # 3 GPUs requested, A10 has 1 and 2 (and 4) → pick 2
        f = match_size_flavour(FLAVOURS, num_gpu=3, gpu_type="A10")
        assert _parse_size_flavour(f["name"])["gpu"] == 2

    def test_gpu_type_filter_case_insensitive(self):
        f = match_size_flavour(FLAVOURS, num_gpu=1, gpu_type="a100")
        assert _parse_size_flavour(f["name"])["gpu_type"] == "A100"

    def test_gpu_type_rtx2080(self):
        f = match_size_flavour(FLAVOURS, num_gpu=2, gpu_type="RTX2080")
        assert f["name"] == "RTX2080 - 2 GPU"

    def test_gpu_no_type_picks_largest_below(self):
        f = match_size_flavour(FLAVOURS, num_gpu=4)
        assert _parse_size_flavour(f["name"])["gpu"] == 4

    def test_gpu_below_all_candidates_uses_smallest_and_warns(self, caplog):
        gpu_flavours = [_size("A10 - 2 GPU"), _size("A10 - 4 GPU (disabled)")]
        import logging
        with caplog.at_level(logging.WARNING, logger="api"):
            f = match_size_flavour(gpu_flavours, num_gpu=1, gpu_type="A10")
        assert _parse_size_flavour(f["name"])["gpu"] == 2
        assert "No size flavour" in caplog.text


class TestBuildCreatePayloadMetaRefs:
    def test_network_refs_include_required_meta_fields(self):
        payload = build_create_payload(
            co={"id": "co-1", "co_name": "CO"},
            wallet={"id": "wallet-1", "name": "Wallet"},
            catalog_item={"icon": "icon"},
            offering={
                "id": "offering-1",
                "application": {"name": "Ray Head Node"},
                "subscription": {
                    "tag": "tag",
                    "name": "SURF HPC Cloud",
                    "subscription_group": {"id": "group-1"},
                },
            },
            os_flavour={"id": "os-1", "name": "Ubuntu 24.04", "category": "os"},
            size_flavour={"id": "size-1", "name": "1 Core - 8 GB RAM", "category": "size"},
            workspace_name="ray-head",
            workspace_description="desc",
            end_time="2026-01-01T00:00:00.000Z",
            host_name="ws-test",
            network_ids=[{"id": "net-1", "name": "ray_network", "type": "network"}],
        )

        assert payload["meta"]["networks"] == [{"id": "net-1", "name": "ray_network", "type": "network"}]

    def test_string_network_id_is_upgraded_to_meta_ref(self):
        payload = build_create_payload(
            co={"id": "co-1", "co_name": "CO"},
            wallet={"id": "wallet-1", "name": "Wallet"},
            catalog_item={"icon": "icon"},
            offering={
                "id": "offering-1",
                "application": {"name": "Ray Head Node"},
                "subscription": {
                    "tag": "tag",
                    "name": "SURF HPC Cloud",
                    "subscription_group": {"id": "group-1"},
                },
            },
            os_flavour={"id": "os-1", "name": "Ubuntu 24.04", "category": "os"},
            size_flavour={"id": "size-1", "name": "1 Core - 8 GB RAM", "category": "size"},
            workspace_name="ray-head",
            workspace_description="desc",
            end_time="2026-01-01T00:00:00.000Z",
            host_name="ws-test",
            network_ids=["net-1"],
        )

        assert payload["meta"]["networks"] == [{"id": "net-1", "name": "net-1", "type": "network"}]


class TestValidateWorkspaceEndTime:
    def test_accepts_future_utc_timestamp(self):
        validate_workspace_end_time("2099-01-01T00:00:00Z")

    def test_rejects_past_timestamp(self):
        with pytest.raises(ValueError, match="must be in the future"):
            validate_workspace_end_time("2020-01-01T00:00:00Z")

    def test_rejects_timestamp_without_timezone(self):
        with pytest.raises(ValueError, match="must include a timezone"):
            validate_workspace_end_time("2099-01-01T00:00:00")


class TestResolveWorkspaceEndTime:
    def test_preserves_supplied_end_time(self):
        assert resolve_workspace_end_time("2099-01-01T00:00:00Z") == "2099-01-01T00:00:00Z"

    def test_defaults_to_three_days_in_future(self):
        now_before = datetime.now(timezone.utc)
        resolved = resolve_workspace_end_time(None)
        now_after = datetime.now(timezone.utc)

        validate_workspace_end_time(resolved)
        parsed = datetime.fromisoformat(resolved.replace("Z", "+00:00"))

        min_expected = now_before + timedelta(days=3) - timedelta(minutes=1)
        max_expected = now_after + timedelta(days=3) + timedelta(minutes=1)
        assert min_expected <= parsed <= max_expected


class TestWorkspaceStatusClassifiers:
    def test_ready_statuses(self):
        assert _is_workspace_ready_status("available")
        assert _is_workspace_ready_status("running")
        assert not _is_workspace_ready_status("creating")

    def test_failure_statuses(self):
        assert _is_workspace_failure_status("failed")
        assert _is_workspace_failure_status("unhealthy")
        assert not _is_workspace_failure_status("updating")
