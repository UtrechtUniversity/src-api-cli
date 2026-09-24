from __future__ import annotations

import random
import string

DEFAULT_RANDOM_SUFFIX_LENGTH = 5


def random_suffix(length: int = DEFAULT_RANDOM_SUFFIX_LENGTH) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def generate_host_name(host_name: str | None, prefix: str) -> str:
    """Return a normalized host name, or a randomly-suffixed one derived from prefix if not supplied."""
    normalized = host_name.strip() if host_name else ""
    return normalized or f"{prefix}_{random_suffix()}"


def generate_resource_name(name: str | None, prefix: str) -> str:
    """Return a normalized resource name, or a randomly-suffixed one derived from prefix if not supplied."""
    normalized = name.strip() if name else ""
    return normalized or f"{prefix}-{random_suffix()}"
