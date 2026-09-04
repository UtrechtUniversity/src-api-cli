from __future__ import annotations

import logging
import re


logger = logging.getLogger(__name__)


def _parse_size_flavour(name: str) -> dict[str, int | str | None]:
    """Parse a size flavour name into CPU/GPU metadata."""
    normalized_name = name.strip()
    cpu = gpu = None
    gpu_type: str | None = None

    gpu_cpu_match = re.match(r"GPU\s+(\d+)\s+Core", normalized_name, re.IGNORECASE)
    if gpu_cpu_match:
        cpu = int(gpu_cpu_match.group(1))

    gpu_count_match = re.search(r"(\d+)x\s+([A-Za-z0-9]+)", normalized_name)
    if gpu_count_match:
        gpu = int(gpu_count_match.group(1))
        gpu_type = gpu_count_match.group(2).upper()

    if gpu is None:
        typed_gpu_match = re.match(r"([A-Za-z0-9]+)\s*-\s*(\d+)\s+GPU", normalized_name, re.IGNORECASE)
        if typed_gpu_match:
            gpu_type = typed_gpu_match.group(1).upper()
            gpu = int(typed_gpu_match.group(2))

    if cpu is None:
        cpu_match = re.match(r"(\d+)\s+[Cc]ore", normalized_name)
        if cpu_match:
            cpu = int(cpu_match.group(1))

    return {"cpu": cpu, "gpu": gpu, "gpu_type": gpu_type}


def match_size_flavour(
    flavours: list[dict],
    num_cpu: int | None = None,
    num_gpu: int | None = None,
    gpu_type: str | None = None,
) -> dict:
    """Return the closest matching size flavour for the requested CPU or GPU count."""
    if (num_cpu is None) == (num_gpu is None):
        raise ValueError("Exactly one of num_cpu or num_gpu must be provided.")

    size_flavours = [flavour for flavour in flavours if flavour.get("category") == "size"]
    if not size_flavours:
        raise ValueError("No size flavours available in this offering.")

    if gpu_type is not None:
        gpu_type_upper = gpu_type.upper()
        typed = [
            flavour
            for flavour in size_flavours
            if (_parse_size_flavour(flavour["name"]).get("gpu_type") or "").upper() == gpu_type_upper
        ]
        if not typed:
            available_types = sorted({
                _parse_size_flavour(flavour["name"]).get("gpu_type") or ""
                for flavour in size_flavours
            } - {""})
            raise ValueError(
                f"No size flavour found for GPU type {gpu_type!r}. Available GPU types: {available_types}"
            )
        size_flavours = typed

    key = "cpu" if num_cpu is not None else "gpu"
    requested = num_cpu if num_cpu is not None else num_gpu

    candidates: list[tuple[int, dict]] = []
    for flavour in size_flavours:
        value = _parse_size_flavour(flavour["name"]).get(key)
        if isinstance(value, int):
            candidates.append((value, flavour))

    if not candidates:
        available_names = [flavour["name"] for flavour in size_flavours]
        raise ValueError(
            f"Could not parse {key!r} count from any size flavour. Available size flavours: {available_names}"
        )

    below = [(value, flavour) for value, flavour in candidates if value <= requested]
    if below:
        _, best = max(below, key=lambda item: item[0])
        return best

    best_value, best = min(candidates, key=lambda item: item[0])
    logger.warning(
        "No size flavour with %s ≤ %d found; using closest available: %r (%s=%d).",
        key,
        requested,
        best["name"],
        key,
        best_value,
    )
    return best
