"""Immutable task-contract construction and initial state."""

from __future__ import annotations

from typing import Any

from .budget import normalize_review_budget, validate_review_budget_mode
from .git_repo import RepoContext, resolve_base_sha, status_snapshot
from .policy import validate_checks, validate_risk
from .store import SCHEMA_VERSION
from .util import (
    InputError,
    new_run_id,
    require_string,
    require_string_list,
    utc_now,
)


DEFAULT_FORBIDDEN_ACTIONS = [
    "push commits or tags",
    "publish or modify a pull request",
    "change Jira or another tracker",
    "deploy or change production infrastructure",
    "run a data migration against shared infrastructure",
    "mutate an external account or contact a real person",
]

DEFAULT_EXECUTION = {
    "native_model": "gpt-5.6-sol",
    "reasoning_effort": "high",
    "escalation_model": None,
}
NATIVE_REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}


def normalize_execution(value: Any) -> dict[str, Any]:
    if value is None:
        return dict(DEFAULT_EXECUTION)
    if not isinstance(value, dict):
        raise InputError("execution must be an object")
    unknown = sorted(set(value) - set(DEFAULT_EXECUTION))
    if unknown:
        raise InputError("execution contains unsupported fields: " + ", ".join(unknown))
    native_model = require_string(
        value.get("native_model", DEFAULT_EXECUTION["native_model"]),
        "execution.native_model",
        maximum=128,
    )
    reasoning_effort = require_string(
        value.get("reasoning_effort", DEFAULT_EXECUTION["reasoning_effort"]),
        "execution.reasoning_effort",
        maximum=32,
    )
    if reasoning_effort not in NATIVE_REASONING_EFFORTS:
        raise InputError("execution.reasoning_effort is unsupported")
    escalation_model = value.get("escalation_model")
    if escalation_model is not None:
        escalation_model = require_string(
            escalation_model, "execution.escalation_model", maximum=128
        )
    return {
        "native_model": native_model,
        "reasoning_effort": reasoning_effort,
        "escalation_model": escalation_model,
    }


def normalize_contract_refs(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise InputError("contract_refs must be an array with at most 64 entries")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"ref", "revision"}:
            raise InputError(
                f"contract_refs[{index}] must contain exactly ref and revision"
            )
        normalized = (
            require_string(item.get("ref"), f"contract_refs[{index}].ref", maximum=1000),
            require_string(
                item.get("revision"),
                f"contract_refs[{index}].revision",
                maximum=256,
            ),
        )
        if normalized not in seen:
            result.append({"ref": normalized[0], "revision": normalized[1]})
            seen.add(normalized)
    return result


def build_contract(
    arguments: dict[str, Any],
    context: RepoContext,
) -> tuple[dict[str, Any], dict[str, Any]]:
    writer = arguments.get("writer", "codex")
    if writer not in ("codex", "claude"):
        raise InputError("writer must be codex or claude")
    writer_explicit = arguments.get("writer_explicit", False)
    if not isinstance(writer_explicit, bool):
        raise InputError("writer_explicit must be a boolean")
    if writer == "claude" and not writer_explicit:
        raise InputError(
            "Claude may be the writer only when writer_explicit is true"
        )

    raw_corrections = arguments.get("max_correction_passes", 2)
    if not isinstance(raw_corrections, int) or isinstance(raw_corrections, bool):
        raise InputError("max_correction_passes must be an integer")
    if not 0 <= raw_corrections <= 8:
        raise InputError("max_correction_passes must be between 0 and 8")
    raw_retries = arguments.get("max_critic_retries", 1)
    if not isinstance(raw_retries, int) or isinstance(raw_retries, bool):
        raise InputError("max_critic_retries must be an integer")
    if raw_retries not in (0, 1):
        raise InputError("max_critic_retries must be 0 or 1")

    dirty = status_snapshot(context)
    allow_dirty = arguments.get("allow_dirty", False)
    if not isinstance(allow_dirty, bool):
        raise InputError("allow_dirty must be a boolean")
    if dirty["dirty"] and not allow_dirty:
        paths = ", ".join(str(path) for path in dirty["paths"][:8])
        suffix = " ..." if len(dirty["paths"]) > 8 else ""
        raise InputError(
            "workspace is dirty; use an isolated worktree before creating "
            f"the run ({paths}{suffix})"
        )

    now = utc_now()
    run_id = new_run_id()
    risk = validate_risk(arguments.get("risk", "medium"))
    review_budget = normalize_review_budget(arguments.get("review_budget"))
    review_budget_mode = validate_review_budget_mode(
        arguments.get("_review_budget_mode")
    )
    base_sha = resolve_base_sha(context, arguments.get("base_sha"))
    frozen_checks = validate_checks(
        arguments.get("required_checks", []), source="contract"
    )
    contract = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "workspace": str(context.workspace),
        "repo_root": str(context.repo_root),
        "git_dir": str(context.git_dir),
        "base_sha": base_sha,
        "initial_worktree": dirty,
        "allow_dirty": allow_dirty,
        "goal": require_string(arguments.get("goal"), "goal"),
        "non_goals": require_string_list(arguments.get("non_goals"), "non_goals"),
        "done_when": require_string_list(
            arguments.get("done_when"), "done_when", allow_empty=False
        ),
        "constraints": require_string_list(
            arguments.get("constraints"), "constraints"
        ),
        "forbidden_actions": list(
            dict.fromkeys(
                [
                    *DEFAULT_FORBIDDEN_ACTIONS,
                    *require_string_list(
                        arguments.get("forbidden_actions"),
                        "forbidden_actions",
                    ),
                ]
            )
        ),
        "writer": writer,
        "writer_explicit": writer_explicit,
        "risk": risk,
        "execution": normalize_execution(arguments.get("execution")),
        "contract_refs": normalize_contract_refs(arguments.get("contract_refs")),
        "review_budget": review_budget,
        "review_budget_mode": review_budget_mode,
        "required_checks": frozen_checks,
        "max_correction_passes": raw_corrections,
        "max_critic_retries": raw_retries,
    }
    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "revision": 0,
        "created_at": now,
        "updated_at": now,
        "phase": "writing",
        "phase_history": [
            {"phase": "prepared", "at": now},
            {"phase": "writing", "at": now},
        ],
        "writer": writer,
        "risk": risk,
        "diff_fingerprint": None,
        "changed_paths": [],
        "diff_stats": None,
        "budget_status": None,
        "planned_checks": [],
        "matched_policy_rules": [],
        "check_results": {},
        "stages": {},
        "correction_passes": 0,
        "critic_retries": 0,
        "review_cycle": 0,
        "review_summary": None,
        "terminal": None,
    }
    return contract, state
