from __future__ import annotations

from collections.abc import Mapping, Sequence


def _normalize_status_filter(status: str | Sequence[str] | None) -> tuple[str, ...]:
    if status is None:
        return ()
    if isinstance(status, str):
        normalized = status.strip()
        return (normalized,) if normalized else ()

    normalized_statuses: list[str] = []
    for item in status:
        if not isinstance(item, str):
            raise ValueError(f"Workspace status filters must be strings, got {type(item).__name__}.")
        normalized = item.strip()
        if normalized:
            normalized_statuses.append(normalized)
    return tuple(normalized_statuses)


def _get_nested_attribute(data: Mapping[str, object], path: str) -> object:
    current: object = data
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
    return current


def _matches_expected_value(actual: object, expected: object) -> bool:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        return all(_matches_expected_value(actual.get(key), value) for key, value in expected.items())

    if isinstance(expected, Sequence) and not isinstance(expected, (str, bytes, bytearray)):
        if not isinstance(actual, Sequence) or isinstance(actual, (str, bytes, bytearray)):
            return False
        return all(any(_matches_expected_value(candidate, value) for candidate in actual) for value in expected)

    return actual == expected


def _matches_attribute_filters(workspace: Mapping[str, object], attribute_filters: Mapping[str, object]) -> bool:
    for path, expected_value in attribute_filters.items():
        if not _matches_expected_value(_get_nested_attribute(workspace, path), expected_value):
            return False
    return True
