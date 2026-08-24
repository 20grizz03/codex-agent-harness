"""Deterministic check planning from frozen and path-based policy."""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .util import CHECK_NAME_RE, InputError, require_string


RISK_RANK = {"low": 0, "medium": 1, "high": 2}
ALWAYS_CHECK = {
    "name": "git-diff-check",
    "argv": ["git", "diff", "--check"],
    "timeout_seconds": 60,
    "source": "harness",
}


def validate_risk(value: Any, name: str = "risk") -> str:
    if value not in RISK_RANK:
        raise InputError(f"{name} must be low, medium, or high")
    return str(value)


def validate_check(value: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError("each check must be an object")
    name = require_string(value.get("name"), "check.name", maximum=80)
    if not CHECK_NAME_RE.fullmatch(name):
        raise InputError("check.name contains unsupported characters")
    argv = value.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 64:
        raise InputError("check.argv must contain 1-64 arguments")
    normalized_argv = [
        require_string(argument, f"check.argv[{index}]", maximum=1_000)
        for index, argument in enumerate(argv)
    ]
    raw_timeout = value.get("timeout_seconds", 600)
    if not isinstance(raw_timeout, int) or isinstance(raw_timeout, bool):
        raise InputError("check.timeout_seconds must be an integer")
    if not 1 <= raw_timeout <= 14_400:
        raise InputError("check.timeout_seconds must be between 1 and 14400")
    return {
        "name": name,
        "argv": normalized_argv,
        "timeout_seconds": raw_timeout,
        "source": source,
    }


def validate_checks(values: Any, *, source: str) -> list[dict[str, Any]]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 64:
        raise InputError("checks must be an array with at most 64 entries")
    return [validate_check(value, source=source) for value in values]


def _load_config(repo_root: Path) -> dict[str, Any]:
    path = repo_root / ".codex" / "agent-harness.json"
    if not path.exists():
        return {"version": 1, "rules": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(".codex/agent-harness.json is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise InputError(".codex/agent-harness.json must use version 1")
    rules = value.get("rules", [])
    if not isinstance(rules, list) or len(rules) > 128:
        raise InputError("policy rules must be an array with at most 128 entries")
    return value


def _normalize_rule(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError(f"policy rule {index} must be an object")
    patterns = value.get("paths")
    if not isinstance(patterns, list) or not 1 <= len(patterns) <= 64:
        raise InputError(f"policy rule {index}.paths must contain 1-64 globs")
    normalized_patterns = [
        require_string(pattern, f"policy rule {index}.paths", maximum=500)
        for pattern in patterns
    ]
    for pattern in normalized_patterns:
        if pattern.startswith("/") or "\x00" in pattern:
            raise InputError("policy path globs must be repository-relative")
    risk = value.get("risk")
    if risk is not None:
        risk = validate_risk(risk, f"policy rule {index}.risk")
    return {
        "paths": normalized_patterns,
        "risk": risk,
        "checks": validate_checks(
            value.get("checks", []), source=f"policy:rule:{index}"
        ),
    }


def _matches(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


def plan_checks(
    *,
    repo_root: Path,
    frozen_checks: list[dict[str, Any]],
    initial_risk: str,
    changed_paths: list[str],
) -> tuple[list[dict[str, Any]], str, list[int]]:
    risk = validate_risk(initial_risk)
    candidates: list[dict[str, Any]] = [dict(ALWAYS_CHECK), *frozen_checks]
    matched_rules: list[int] = []
    config = _load_config(repo_root)
    for index, raw_rule in enumerate(config.get("rules", [])):
        rule = _normalize_rule(raw_rule, index)
        if not any(_matches(path, rule["paths"]) for path in changed_paths):
            continue
        matched_rules.append(index)
        candidates.extend(rule["checks"])
        if rule["risk"] is not None and RISK_RANK[rule["risk"]] > RISK_RANK[risk]:
            risk = rule["risk"]

    result: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        normalized = validate_check(
            candidate, source=str(candidate.get("source", "contract"))
        )
        existing = by_name.get(normalized["name"])
        if existing is None:
            by_name[normalized["name"]] = normalized
            result.append(normalized)
            continue
        comparable = (existing["argv"], existing["timeout_seconds"])
        new_comparable = (normalized["argv"], normalized["timeout_seconds"])
        if comparable != new_comparable:
            raise InputError(
                f"check {normalized['name']} has conflicting definitions"
            )
    return result, risk, matched_rules
