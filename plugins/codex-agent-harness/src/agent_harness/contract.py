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

    raw_corrections = arguments.get("max_correction_passes", 1)
    if (
        not isinstance(raw_corrections, int)
        or isinstance(raw_corrections, bool)
        or raw_corrections not in (0, 1)
    ):
        raise InputError("max_correction_passes must be 0 or 1 in v1")

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
        "review_budget": review_budget,
        "review_budget_mode": review_budget_mode,
        "required_checks": frozen_checks,
        "max_correction_passes": raw_corrections,
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
        "review_summary": None,
        "terminal": None,
    }
    return contract, state
