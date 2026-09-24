from __future__ import annotations

from datetime import datetime, timedelta, timezone

DEFAULT_WORKSPACE_ENDTIME = timedelta(days=3)


def validate_workspace_end_time(end_time: str) -> None:
    """Raise ValueError if end_time is not a future, timezone-aware ISO 8601 datetime."""
    normalized = end_time.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            "end_time must be a valid ISO 8601 datetime (for example 2026-12-31T23:59:59Z)."
        ) from exc

    if parsed.tzinfo is None:
        raise ValueError("end_time must include a timezone (use trailing 'Z' for UTC).")

    now_utc = datetime.now(timezone.utc)
    if parsed <= now_utc:
        raise ValueError(
            f"end_time must be in the future. Current UTC time is "
            f"{now_utc.isoformat().replace('+00:00', 'Z')}."
        )


def resolve_workspace_end_time(end_time: str | None) -> str:
    """Return end_time if supplied, otherwise a default of DEFAULT_WORKSPACE_ENDTIME from now."""
    if end_time and end_time.strip():
        return end_time.strip()
    return (datetime.now(timezone.utc) + DEFAULT_WORKSPACE_ENDTIME).strftime("%Y-%m-%dT%H:%M:%SZ")
