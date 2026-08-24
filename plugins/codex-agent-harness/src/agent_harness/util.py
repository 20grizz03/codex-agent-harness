"""Shared validation, identifiers, timestamps, and bounded redaction."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Iterable


RUN_ID_RE = re.compile(r"^run-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}$")
STAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CHECK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
FINDING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,})\b"
)


class HarnessError(RuntimeError):
    """Base error safe to expose through MCP."""


class InputError(HarnessError):
    """The caller supplied an invalid contract or tool argument."""


class StateError(HarnessError):
    """Persisted state is missing, corrupt, or violates the lifecycle."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{timestamp}-{uuid.uuid4().hex[:12]}"


def require_string(
    value: Any,
    name: str,
    *,
    minimum: int = 1,
    maximum: int = 12_000,
) -> str:
    if not isinstance(value, str):
        raise InputError(f"{name} must be a string")
    normalized = value.strip()
    if len(normalized) < minimum or len(normalized) > maximum:
        raise InputError(
            f"{name} must contain between {minimum} and {maximum} characters"
        )
    if "\x00" in normalized:
        raise InputError(f"{name} must not contain NUL bytes")
    return normalized


def require_string_list(
    value: Any,
    name: str,
    *,
    maximum_items: int = 64,
    item_maximum: int = 1_000,
    allow_empty: bool = True,
) -> list[str]:
    if value is None and allow_empty:
        return []
    if not isinstance(value, list):
        raise InputError(f"{name} must be an array of strings")
    if len(value) > maximum_items:
        raise InputError(f"{name} must contain at most {maximum_items} items")
    result = [
        require_string(item, f"{name}[{index}]", maximum=item_maximum)
        for index, item in enumerate(value)
    ]
    if not allow_empty and not result:
        raise InputError(f"{name} must not be empty")
    return result


def sanitize_text(value: Any, *, maximum: int = 2_000) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\x00", "").strip()
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1\2<redacted>", text)
    text = _TOKEN_RE.sub("<redacted>", text)
    return text[:maximum]


def sanitize_string_list(
    values: Any,
    *,
    maximum_items: int = 64,
    item_maximum: int = 1_000,
) -> list[str]:
    if not isinstance(values, list):
        return []
    return [
        cleaned
        for value in values[:maximum_items]
        if (cleaned := sanitize_text(value, maximum=item_maximum))
    ]


def numeric_tree(value: Any, *, depth: int = 0) -> Any:
    """Keep only bounded numeric telemetry and safe dictionary keys."""
    if depth > 4:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", key_text):
                continue
            normalized = numeric_tree(child, depth=depth + 1)
            if normalized is not None:
                result[key_text] = normalized
        return result
    return None


def json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def unique_in_order(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
