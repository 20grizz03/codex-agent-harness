"""Durable epic campaign contracts and private Git-metadata storage."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contract import DEFAULT_FORBIDDEN_ACTIONS
from .git_repo import RepoContext, resolve_repo, status_snapshot
from .policy import validate_risk
from .store import RunStore, SCHEMA_VERSION
from .util import (
    InputError,
    StateError,
    json_copy,
    require_string,
    require_string_list,
    sanitize_string_list,
    sanitize_text,
    utc_now,
)


CAMPAIGN_ID_RE = re.compile(
    r"^campaign-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}$"
)
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
SHA_RE = re.compile(r"^[a-fA-F0-9]{7,64}$")

CAMPAIGN_MODES = {"delivery", "replay"}
SOURCE_KINDS = {"jira", "local"}
TASK_KINDS = {"implementation", "analysis", "delivery"}
TASK_STATUSES = {
    "pending",
    "in_progress",
    "complete",
    "needs_human",
    "blocked",
    "failed",
    "interrupted",
}
CAMPAIGN_TERMINAL_PHASES = {
    "complete",
    "needs_human",
    "blocked",
    "failed",
    "interrupted",
}
COMPARISON_DIMENSIONS = (
    "scope",
    "behavior",
    "architecture",
    "tests",
    "operability",
)

REPLAY_WITHHELD_EVIDENCE = [
    "поля задачи и комментарии, добавленные после момента отсечения",
    "решение задачи и итоговые статусы",
    "рекомендации к тестированию, добавленные после реализации",
    "связанные PR, коммиты и метаданные разработки",
    "исторические изменения кода и детали реализации",
]


def new_campaign_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"campaign-{timestamp}-{uuid.uuid4().hex[:12]}"


def _exact_object(
    value: Any,
    name: str,
    *,
    allowed: set[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InputError(f"{name} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise InputError(f"{name} contains unsupported fields: {', '.join(unknown)}")
    return value


def _cutoff(value: Any) -> str:
    cutoff = require_string(value, "cutoff_at", maximum=64)
    try:
        parsed = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError("cutoff_at must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise InputError("cutoff_at must include a timezone")
    return cutoff


def _sanitized_string(
    value: Any,
    name: str,
    *,
    maximum: int = 12_000,
) -> str:
    return sanitize_text(
        require_string(value, name, maximum=maximum),
        maximum=maximum,
    )


def _sanitized_string_list(
    value: Any,
    name: str,
    *,
    allow_empty: bool = True,
) -> list[str]:
    validated = require_string_list(
        value,
        name,
        allow_empty=allow_empty,
    )
    return sanitize_string_list(validated)


def _source(value: Any) -> dict[str, str]:
    source = _exact_object(value, "source", allowed={"kind", "ref"})
    kind = require_string(source.get("kind"), "source.kind", maximum=32)
    if kind not in SOURCE_KINDS:
        raise InputError("source.kind must be jira or local")
    return {
        "kind": kind,
        "ref": _sanitized_string(
            source.get("ref"), "source.ref", maximum=2_000
        ),
    }


def _tasks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise InputError("tasks must contain between 1 and 64 entries")
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        task = _exact_object(
            raw,
            f"tasks[{index}]",
            allowed={
                "id",
                "title",
                "goal",
                "done_when",
                "kind",
                "dependencies",
                "workspace",
                "base_sha",
            },
        )
        task_id = require_string(task.get("id"), f"tasks[{index}].id", maximum=80)
        if not TASK_ID_RE.fullmatch(task_id):
            raise InputError(f"tasks[{index}].id has an invalid format")
        if task_id in seen:
            raise InputError(f"duplicate task id: {task_id}")
        kind = require_string(
            task.get("kind", "implementation"),
            f"tasks[{index}].kind",
            maximum=32,
        )
        if kind not in TASK_KINDS:
            raise InputError(
                f"tasks[{index}].kind must be implementation, analysis, or delivery"
            )
        dependencies = require_string_list(
            task.get("dependencies"),
            f"tasks[{index}].dependencies",
            item_maximum=80,
        )
        unknown_dependencies = [item for item in dependencies if item not in seen]
        if unknown_dependencies:
            raise InputError(
                f"tasks[{index}].dependencies must reference earlier tasks: "
                + ", ".join(unknown_dependencies)
            )
        normalized: dict[str, Any] = {
            "id": task_id,
            "title": _sanitized_string(
                task.get("title"), f"tasks[{index}].title", maximum=300
            ),
            "goal": _sanitized_string(
                task.get("goal"), f"tasks[{index}].goal", maximum=12_000
            ),
            "done_when": _sanitized_string_list(
                task.get("done_when"),
                f"tasks[{index}].done_when",
                allow_empty=False,
            ),
            "kind": kind,
            "dependencies": dependencies,
        }
        if task.get("workspace") is not None:
            normalized["workspace"] = require_string(
                task.get("workspace"),
                f"tasks[{index}].workspace",
                maximum=4_096,
            )
        if task.get("base_sha") is not None:
            base_sha = require_string(
                task.get("base_sha"),
                f"tasks[{index}].base_sha",
                maximum=64,
            )
            if not SHA_RE.fullmatch(base_sha):
                raise InputError(f"tasks[{index}].base_sha is invalid")
            normalized["base_sha"] = base_sha.lower()
        tasks.append(normalized)
        seen.add(task_id)
    return tasks


def build_campaign(
    arguments: Mapping[str, Any],
    context: RepoContext,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _exact_object(
        arguments,
        "campaign",
        allowed={
            "workspace",
            "title",
            "goal",
            "done_when",
            "non_goals",
            "constraints",
            "forbidden_actions",
            "risk",
            "mode",
            "source",
            "cutoff_at",
            "tasks",
        },
    )
    mode = require_string(arguments.get("mode", "delivery"), "mode", maximum=32)
    if mode not in CAMPAIGN_MODES:
        raise InputError("mode must be delivery or replay")
    cutoff_at = _cutoff(arguments.get("cutoff_at")) if mode == "replay" else None
    if mode == "delivery" and arguments.get("cutoff_at") is not None:
        raise InputError("cutoff_at is only valid for replay campaigns")

    now = utc_now()
    campaign_id = new_campaign_id()
    tasks = _tasks(arguments.get("tasks"))
    for task in tasks:
        if task["kind"] != "implementation":
            continue
        task_context = (
            resolve_repo(task["workspace"])
            if "workspace" in task
            else context
        )
        task["workspace"] = str(task_context.repo_root)
        task.setdefault("base_sha", task_context.head_sha)

    initial_worktree = status_snapshot(context)
    if initial_worktree["dirty"]:
        paths = ", ".join(str(path) for path in initial_worktree["paths"][:8])
        suffix = " ..." if len(initial_worktree["paths"]) > 8 else ""
        raise InputError(
            "campaign workspace is dirty; use an isolated worktree before "
            f"creating the campaign ({paths}{suffix})"
        )
    contract = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "created_at": now,
        "workspace": str(context.workspace),
        "repo_root": str(context.repo_root),
        "git_dir": str(context.git_dir),
        "base_sha": context.head_sha,
        "initial_worktree": initial_worktree,
        "title": _sanitized_string(
            arguments.get("title"), "title", maximum=300
        ),
        "goal": _sanitized_string(arguments.get("goal"), "goal"),
        "done_when": _sanitized_string_list(
            arguments.get("done_when"), "done_when", allow_empty=False
        ),
        "non_goals": _sanitized_string_list(
            arguments.get("non_goals"), "non_goals"
        ),
        "constraints": _sanitized_string_list(
            arguments.get("constraints"), "constraints"
        ),
        "forbidden_actions": list(
            dict.fromkeys(
                [
                    *DEFAULT_FORBIDDEN_ACTIONS,
                    *_sanitized_string_list(
                        arguments.get("forbidden_actions"), "forbidden_actions"
                    ),
                ]
            )
        ),
        "risk": validate_risk(arguments.get("risk", "high")),
        "mode": mode,
        "source": _source(arguments.get("source")),
        "cutoff_at": cutoff_at,
        "withheld_evidence": REPLAY_WITHHELD_EVIDENCE if mode == "replay" else [],
        "tasks": tasks,
        "interaction_policy": (
            "задавать вопросы только при блокирующем продуктовом выборе "
            "или внешнем изменении"
        ),
    }
    state = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "revision": 0,
        "created_at": now,
        "updated_at": now,
        "phase": "executing",
        "phase_history": [
            {"phase": "prepared", "at": now},
            {"phase": "executing", "at": now},
        ],
        "tasks": {
            task["id"]: {
                "status": "pending",
                "summary": "",
                "run": None,
                "updated_at": now,
            }
            for task in tasks
        },
        "interventions": {},
        "candidate": None,
        "terminal": None,
    }
    return contract, state


class CampaignStore:
    """Campaign storage alongside runs without exposing it in the worktree."""

    def __init__(self, context: RepoContext) -> None:
        self.context = context
        self.root = context.git_dir / "codex-agent-harness" / "campaigns"

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> "CampaignStore":
        from .git_repo import resolve_repo

        return cls(resolve_repo(workspace))

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root.parent, 0o700)
        os.chmod(self.root, 0o700)

    def campaign_dir(self, campaign_id: str) -> Path:
        if not CAMPAIGN_ID_RE.fullmatch(campaign_id):
            raise StateError("invalid campaign_id")
        path = self.root / campaign_id
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError as exc:
            raise StateError("campaign path escaped the state root") from exc
        return path

    def create(self, contract: dict[str, Any], state: dict[str, Any]) -> None:
        self._ensure_root()
        campaign_id = str(contract["campaign_id"])
        directory = self.campaign_dir(campaign_id)
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise StateError("campaign_id already exists") from exc
        os.chmod(directory, 0o700)
        RunStore._create_json(directory / "contract.json", contract)
        RunStore._create_json(directory / "state.json", state)
        RunStore._create_json(
            directory / "comparison.json",
            {
                "schema_version": SCHEMA_VERSION,
                "campaign_id": campaign_id,
                "comparison": None,
            },
        )
        descriptor = os.open(
            directory / "events.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(descriptor)
        self.append_event(campaign_id, {"type": "campaign_created"})

    def _read(self, campaign_id: str, name: str) -> dict[str, Any]:
        value = RunStore._read_json(self.campaign_dir(campaign_id) / name)
        if value.get("campaign_id") != campaign_id:
            raise StateError(f"{name} campaign_id mismatch")
        if value.get("schema_version") != SCHEMA_VERSION:
            raise StateError(f"unsupported {name} schema_version")
        return value

    def read_contract(self, campaign_id: str) -> dict[str, Any]:
        return self._read(campaign_id, "contract.json")

    def read_state(self, campaign_id: str) -> dict[str, Any]:
        return self._read(campaign_id, "state.json")

    def save_state(
        self, campaign_id: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        current = json_copy(state)
        current["revision"] = int(current.get("revision", 0)) + 1
        current["updated_at"] = utc_now()
        RunStore._atomic_json(
            self.campaign_dir(campaign_id) / "state.json", current
        )
        return current

    def read_comparison(self, campaign_id: str) -> dict[str, Any]:
        return self._read(campaign_id, "comparison.json")

    def save_comparison(
        self, campaign_id: str, comparison: dict[str, Any]
    ) -> None:
        RunStore._atomic_json(
            self.campaign_dir(campaign_id) / "comparison.json", comparison
        )

    def append_event(self, campaign_id: str, event: dict[str, Any]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "campaign_id": campaign_id,
            "at": utc_now(),
            **json_copy(event),
        }
        path = self.campaign_dir(campaign_id) / "events.jsonl"
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            with os.fdopen(descriptor, "ab", closefd=False) as stream:
                stream.write(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                    + b"\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)

    def list_campaign_ids(self) -> Iterable[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            (
                child.name
                for child in self.root.iterdir()
                if child.is_dir() and CAMPAIGN_ID_RE.fullmatch(child.name)
            ),
            reverse=True,
        )
