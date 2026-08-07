"""Validation helpers for environment mappings persisted by Pyruns."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Dict


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_valid_environment_name(name: str) -> bool:
    """Return whether *name* is accepted by subprocess environment APIs."""

    return bool(_ENV_NAME_RE.fullmatch(str(name or "")))


def normalize_environment(
    values: Mapping[Any, Any] | None,
    *,
    drop_none_values: bool = False,
) -> Dict[str, str]:
    """Normalize and validate an environment mapping before it is persisted or used."""

    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise ValueError("environment must be an object")

    result: Dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key = str(raw_key).strip()
        if not is_valid_environment_name(key):
            raise ValueError(f"invalid environment variable name: {key or raw_key!s}")
        if key in result:
            raise ValueError(f"duplicate environment variable name: {key}")
        if raw_value is None and drop_none_values:
            continue

        value = str(raw_value)
        if "\x00" in value:
            raise ValueError(f"environment variable '{key}' contains a null byte")
        result[key] = value
    return result
