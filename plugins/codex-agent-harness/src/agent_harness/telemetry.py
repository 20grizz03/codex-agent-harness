"""Allowlisted, explicitly reported native Codex usage observations.

The MCP runtime does not expose Codex token counters. This module accepts only
structured cumulative snapshots supplied by a caller that can see them.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .util import InputError, StateError, sanitize_text, utc_now


COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
ROLES = {"lead", "executor", "reviewer"}
PATHS = {"primary", "retry", "fallback"}
AVAILABILITY = {"available", "partial", "unavailable"}
REASONS = {"not_exposed", "interrupted", "incomplete_snapshot", "other"}
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
MAX_COUNTER = 10**15
MAX_OBSERVATIONS = 128


def _label(value: Any, name: str) -> str:
    if (not isinstance(value, str) or not LABEL.fullmatch(value)
            or sanitize_text(value, maximum=80) != value):
        raise InputError(f"{name} must be a safe identifier of at most 80 characters")
    return value


def _counter_snapshot(value: Any, name: str) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not value:
        raise InputError(f"{name} must be a nonempty counter object")
    unknown = set(value) - set(COUNTERS)
    if unknown:
        raise InputError(f"{name} has unsupported counter names")
    result = {}
    for key, count in value.items():
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_COUNTER:
            raise InputError(f"{name}.{key} must be a bounded nonnegative integer")
        result[key] = count
    return result


def normalize_observation(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the exact persistable shape; never retain arbitrary provider data."""
    allowed = {
        "workspace", "target_type", "target_id", "observation_id", "role",
        "attempt_id", "path", "scope_id", "availability", "reason",
        "baseline", "final",
    }
    if set(arguments) - allowed:
        raise InputError("telemetry contains unsupported fields")
    observation_id = _label(arguments.get("observation_id"), "observation_id")
    role = arguments.get("role")
    path = arguments.get("path")
    availability = arguments.get("availability")
    if (not isinstance(role, str) or role not in ROLES
            or not isinstance(path, str) or path not in PATHS
            or not isinstance(availability, str) or availability not in AVAILABILITY):
        raise InputError("invalid telemetry role, path, or availability")
    attempt_id = _label(arguments.get("attempt_id"), "attempt_id")
    scope_id = _label(arguments.get("scope_id"), "scope_id")
    baseline = _counter_snapshot(arguments.get("baseline"), "baseline")
    final = _counter_snapshot(arguments.get("final"), "final")
    reason = arguments.get("reason")
    if availability == "unavailable":
        if (baseline is not None or final is not None
                or not isinstance(reason, str) or reason not in REASONS):
            raise InputError("unavailable telemetry needs a reason and no counters")
        delta = None
    else:
        if baseline is None and final is None:
            raise InputError("partial or available telemetry needs a counter snapshot")
        if reason is not None and (not isinstance(reason, str) or reason not in REASONS):
            raise InputError("invalid telemetry reason")
        shared = set(baseline or {}) & set(final or {})
        delta = {}
        for key in sorted(shared):
            difference = final[key] - baseline[key]
            if difference < 0:
                raise InputError("final counters cannot be below baseline")
            delta[key] = difference
        complete = (baseline is not None and final is not None
                    and set(baseline) == set(final)
                    and {"input_tokens", "output_tokens"} <= set(delta))
        if availability == "available" and not complete:
            raise InputError("available telemetry requires matching input/output baseline and final")
        if availability == "partial" and complete:
            raise InputError("complete counter pair must be marked available")
        if availability == "partial" and reason is None:
            raise InputError("partial telemetry needs a reason")
        if availability == "available" and reason is not None:
            raise InputError("available telemetry cannot have a missing-data reason")
    return {
        "observation_id": observation_id,
        "role": role,
        "attempt_id": attempt_id,
        "path": path,
        "scope_id": scope_id,
        "availability": availability,
        "reason": reason,
        "baseline": baseline,
        "final": final,
        "delta": delta,
        "source": "caller_reported_native_codex_cumulative",
    }


def record(state: dict[str, Any], observation: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    ledger = state.setdefault("codex_telemetry", {"observations": []})
    entries = ledger["observations"]
    if not isinstance(entries, list):
        raise StateError("invalid codex telemetry ledger")
    for previous in entries:
        if previous.get("observation_id") == observation["observation_id"]:
            if {key: value for key, value in previous.items() if key != "recorded_at"} == observation:
                return previous, True
            raise StateError("observation_id already has different telemetry")
        if (previous.get("role"), previous.get("attempt_id"), previous.get("path")) == (
            observation["role"], observation["attempt_id"], observation["path"]
        ):
            raise StateError("this role, attempt, and path already has an observation")
        if previous.get("scope_id") == observation["scope_id"]:
            old_baseline, old_final = previous.get("baseline"), previous.get("final")
            baseline, final = observation["baseline"], observation["final"]
            if old_baseline and old_final and baseline and final:
                for key in set(old_baseline) & set(old_final) & set(baseline) & set(final):
                    if max(old_baseline[key], baseline[key]) < min(old_final[key], final[key]):
                        raise StateError("cumulative telemetry intervals overlap in one scope")
    if len(entries) >= MAX_OBSERVATIONS:
        raise StateError("codex telemetry observation limit reached")
    entry = {**observation, "recorded_at": utc_now()}
    entries.append(entry)
    return entry, False


def summary(state: Mapping[str, Any]) -> dict[str, Any]:
    ledger = state.get("codex_telemetry", {})
    entries = ledger.get("observations", []) if isinstance(ledger, Mapping) else []
    by_role: dict[str, dict[str, Any]] = {}
    for role in sorted(ROLES):
        selected = [entry for entry in entries if entry.get("role") == role]
        observed_keys = set().union(*(entry.get("delta", {}) or {} for entry in selected))
        totals = {key: sum((entry.get("delta") or {}).get(key, 0) for entry in selected)
                  for key in COUNTERS if key in observed_keys}
        by_role[role] = {
            "observations": len(selected),
            "available": sum(entry.get("availability") == "available" for entry in selected),
            "partial": sum(entry.get("availability") == "partial" for entry in selected),
            "unavailable": sum(entry.get("availability") == "unavailable" for entry in selected),
            "observed_token_deltas": totals,
        }
    return {
        "coverage": ("unavailable" if not entries or all(e.get("availability") == "unavailable" for e in entries)
                     else "partial" if any(e.get("availability") != "available" for e in entries)
                     else "available"),
        "by_role": by_role,
        "cost": None,
    }
