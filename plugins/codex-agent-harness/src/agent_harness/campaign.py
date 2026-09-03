"""Durable epic campaign contracts and private Git-metadata storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .budget import normalize_review_budget
from .contract import DEFAULT_FORBIDDEN_ACTIONS
from .git_repo import RepoContext, resolve_repo, run_git, status_snapshot
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
OPENSPEC_CHANGE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
OPENSPEC_ARCHIVE_RE_TEMPLATE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-%s$"
OPENSPEC_CHECKBOX_RE = re.compile(rb"(?m)^([ \t]*-[ \t]+)\[[ xX]\]")
OPENSPEC_SCHEMA_RE = re.compile(
    rb"(?m)^[ \t]*schema:[ \t]*agent-harness[ \t]*(?:#.*)?$"
)

OPENSPEC_PROPOSAL_HEADINGS = (
    "Why",
    "What Changes",
    "Non-Goals",
    "Impact",
    "Open Questions",
)
OPENSPEC_DESIGN_HEADINGS = (
    "Security and Data",
    "Failure and Recovery",
    "Operability",
    "Compatibility",
    "UI and Source Material",
)

MAX_OPENSPEC_FILES = 512
MAX_OPENSPEC_BYTES = 8 * 1024 * 1024

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


def _repo_local(path: Path, repo_root: Path, name: str) -> None:
    try:
        path.resolve().relative_to(repo_root.resolve())
    except ValueError as exc:
        raise InputError(f"{name} escaped the repository") from exc


def _openspec_layout(context: RepoContext) -> tuple[Path, Path]:
    openspec_root = context.repo_root / "openspec"
    changes_root = openspec_root / "changes"
    config = openspec_root / "config.yaml"
    if openspec_root.is_symlink() or changes_root.is_symlink():
        raise InputError("OpenSpec directories must not be symlinks")
    if not config.is_file() or config.is_symlink():
        raise InputError(
            "OpenSpec must already be initialized with openspec/config.yaml"
        )
    if not changes_root.is_dir():
        raise InputError("OpenSpec changes directory does not exist")
    for path, name in (
        (openspec_root, "openspec"),
        (changes_root, "openspec/changes"),
        (config, "openspec/config.yaml"),
    ):
        _repo_local(path, context.repo_root, name)
    return openspec_root, changes_root


def _openspec_change_dir(
    context: RepoContext,
    change_id: str,
    *,
    allow_archive: bool,
) -> Path:
    _, changes_root = _openspec_layout(context)
    active = changes_root / change_id
    if active.is_symlink():
        raise InputError("OpenSpec change directory must not be a symlink")
    if active.is_dir():
        _repo_local(active, context.repo_root, "OpenSpec change")
        return active
    if not allow_archive:
        raise InputError(f"OpenSpec change does not exist: {change_id}")

    archive_root = changes_root / "archive"
    if archive_root.is_symlink():
        raise InputError("OpenSpec archive directory must not be a symlink")
    if not archive_root.is_dir():
        raise InputError(
            f"OpenSpec change is neither active nor archived: {change_id}"
        )
    _repo_local(archive_root, context.repo_root, "OpenSpec archive")
    archive_name = re.compile(
        OPENSPEC_ARCHIVE_RE_TEMPLATE % re.escape(change_id)
    )
    candidates = [
        path
        for path in archive_root.iterdir()
        if archive_name.fullmatch(path.name)
    ]
    if len(candidates) != 1:
        raise InputError(
            f"OpenSpec archive must contain exactly one change: {change_id}"
        )
    archived = candidates[0]
    if archived.is_symlink() or not archived.is_dir():
        raise InputError("OpenSpec archived change must be a real directory")
    _repo_local(archived, context.repo_root, "OpenSpec archived change")
    return archived


def _openspec_directory_files(change_dir: Path) -> dict[str, bytes]:
    if change_dir.is_symlink() or not change_dir.is_dir():
        raise InputError("OpenSpec change must be a real directory")
    files: dict[str, bytes] = {}
    total_bytes = 0
    for path in sorted(
        change_dir.rglob("*"),
        key=lambda item: item.relative_to(change_dir).as_posix(),
    ):
        if path.is_symlink():
            raise InputError("OpenSpec change must not contain symlinks")
        if path.is_dir():
            continue
        if not path.is_file():
            raise InputError("OpenSpec change contains a non-file entry")
        total_bytes += path.stat().st_size
        if len(files) >= MAX_OPENSPEC_FILES or total_bytes > MAX_OPENSPEC_BYTES:
            raise InputError("OpenSpec change is too large to freeze safely")
        files[path.relative_to(change_dir).as_posix()] = path.read_bytes()
    return files


def _openspec_fingerprint_files(relative_files: Mapping[str, bytes]) -> str:
    total_bytes = sum(len(content) for content in relative_files.values())
    if (
        len(relative_files) > MAX_OPENSPEC_FILES
        or total_bytes > MAX_OPENSPEC_BYTES
    ):
        raise InputError("OpenSpec change is too large to freeze safely")

    missing = [
        name
        for name in (".openspec.yaml", "proposal.md", "design.md", "tasks.md")
        if name not in relative_files
    ]
    spec_files = [
        name
        for name in relative_files
        if name.startswith("specs/") and name.endswith(".md")
    ]
    if not spec_files:
        missing.append("specs/**/*.md")
    if missing:
        raise InputError(
            "OpenSpec change is incomplete; missing: " + ", ".join(missing)
        )

    metadata = relative_files[".openspec.yaml"]
    if not OPENSPEC_SCHEMA_RE.search(metadata):
        raise InputError("OpenSpec change must use schema: agent-harness")

    for relative, headings in (
        ("proposal.md", OPENSPEC_PROPOSAL_HEADINGS),
        ("design.md", OPENSPEC_DESIGN_HEADINGS),
    ):
        try:
            content = relative_files[relative].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InputError(f"OpenSpec {relative} must be valid UTF-8") from exc
        absent = [
            heading
            for heading in headings
            if not re.search(rf"(?m)^## {re.escape(heading)}[ \t]*$", content)
        ]
        if absent:
            raise InputError(
                f"OpenSpec {relative} is missing required headings: "
                + ", ".join(absent)
            )

    digest = hashlib.sha256()
    digest.update(b"agent-harness-openspec-v1\0")
    for relative in sorted(relative_files):
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        content = relative_files[relative]
        if relative == "tasks.md":
            content = OPENSPEC_CHECKBOX_RE.sub(rb"\1[ ]", content)
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _openspec_fingerprint(change_dir: Path) -> str:
    return _openspec_fingerprint_files(_openspec_directory_files(change_dir))


def _require_ignored_openspec(context: RepoContext, change_dir: Path) -> None:
    for path in (
        context.repo_root / "openspec/config.yaml",
        change_dir / ".openspec.yaml",
    ):
        relative = path.relative_to(context.repo_root).as_posix()
        completed = run_git(
            context.repo_root,
            ["check-ignore", "--quiet", "--", relative],
        )
        if completed.returncode == 1:
            raise InputError(
                "local OpenSpec must be ignored by Git; add /openspec/ to "
                ".git/info/exclude or .gitignore, or use storage=repository "
                "for tracked OpenSpec"
            )
        if completed.returncode != 0:
            raise InputError(
                "unable to verify that local OpenSpec is ignored by Git"
            )


def _openspec_reference(
    value: Any,
    context: RepoContext,
) -> tuple[dict[str, str] | None, dict[str, bytes] | None]:
    if value is None:
        return None, None
    spec = _exact_object(
        value,
        "spec",
        allowed={"kind", "change_id", "storage"},
    )
    kind = require_string(spec.get("kind"), "spec.kind", maximum=32)
    if kind != "openspec":
        raise InputError("spec.kind must be openspec")
    change_id = require_string(
        spec.get("change_id"), "spec.change_id", maximum=128
    )
    if not OPENSPEC_CHANGE_RE.fullmatch(change_id):
        raise InputError("spec.change_id must be lowercase kebab-case")
    storage = require_string(
        spec.get("storage", "local"), "spec.storage", maximum=32
    )
    if storage not in {"local", "repository"}:
        raise InputError("spec.storage must be local or repository")
    change_dir = _openspec_change_dir(
        context,
        change_id,
        allow_archive=False,
    )
    files = _openspec_directory_files(change_dir)
    if storage == "local":
        _require_ignored_openspec(context, change_dir)

    return (
        {
            "kind": kind,
            "change_id": change_id,
            "storage": storage,
            "path": change_dir.relative_to(context.repo_root).as_posix(),
            "sha256": _openspec_fingerprint_files(files),
        },
        files if storage == "local" else None,
    )


def verify_openspec_reference(
    contract: Mapping[str, Any],
    context: RepoContext,
    local_spec_dir: Path | None = None,
) -> None:
    reference = contract.get("spec")
    if reference is None:
        return
    old_fields = {"kind", "change_id", "path", "sha256"}
    new_fields = {*old_fields, "storage"}
    if not isinstance(reference, Mapping) or frozenset(reference) not in {
        frozenset(old_fields),
        frozenset(new_fields),
    }:
        raise StateError("campaign OpenSpec reference is corrupt")
    try:
        kind = require_string(reference.get("kind"), "spec.kind", maximum=32)
        change_id = require_string(
            reference.get("change_id"), "spec.change_id", maximum=128
        )
        storage = require_string(
            reference.get("storage", "repository"),
            "spec.storage",
            maximum=32,
        )
        expected_path = f"openspec/changes/{change_id}"
        if (
            kind != "openspec"
            or not OPENSPEC_CHANGE_RE.fullmatch(change_id)
            or storage not in {"local", "repository"}
            or reference.get("path") != expected_path
        ):
            raise InputError("invalid stored OpenSpec reference")
        if storage == "local":
            if local_spec_dir is None:
                raise InputError("local OpenSpec snapshot path is unavailable")
            snapshot = _openspec_fingerprint(local_spec_dir)
            repo_root = require_string(
                contract.get("repo_root"), "repo_root", maximum=4_096
            )
            spec_context = resolve_repo(repo_root)
            if spec_context.git_common_dir != context.git_common_dir:
                raise InputError(
                    "campaign OpenSpec belongs to a different Git repository"
                )
            change_dir = _openspec_change_dir(
                spec_context,
                change_id,
                allow_archive=False,
            )
            _require_ignored_openspec(spec_context, change_dir)
            current = _openspec_fingerprint(change_dir)
        else:
            change_dir = _openspec_change_dir(
                context,
                change_id,
                allow_archive=True,
            )
            current = _openspec_fingerprint(change_dir)
    except InputError as exc:
        raise StateError(f"cannot verify approved OpenSpec change: {exc}") from exc
    if reference.get("sha256") != current or (
        storage == "local" and reference.get("sha256") != snapshot
    ):
        raise StateError(
            "OpenSpec change differs from the approved campaign specification"
        )


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
                "base_from_task",
                "role",
                "review_budget",
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
        role = require_string(
            task.get("role", "task"),
            f"tasks[{index}].role",
            maximum=32,
        )
        if role not in {"task", "integration", "finalizer"}:
            raise InputError(
                f"tasks[{index}].role must be task, integration, or finalizer"
            )
        if kind != "implementation" and role != "task":
            raise InputError(
                f"tasks[{index}].role is only valid for implementation tasks"
            )
        normalized["role"] = role
        if task.get("review_budget") is not None and kind != "implementation":
            raise InputError(
                f"tasks[{index}].review_budget is only valid for implementation tasks"
            )
        if kind == "implementation":
            normalized["review_budget"] = normalize_review_budget(
                task.get("review_budget")
            )
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
        if task.get("base_from_task") is not None:
            base_from_task = require_string(
                task.get("base_from_task"),
                f"tasks[{index}].base_from_task",
                maximum=80,
            )
            if kind != "implementation":
                raise InputError(
                    f"tasks[{index}].base_from_task requires implementation kind"
                )
            if base_from_task not in dependencies:
                raise InputError(
                    f"tasks[{index}].base_from_task must be a dependency"
                )
            if "base_sha" in normalized:
                raise InputError(
                    f"tasks[{index}] cannot set both base_sha and base_from_task"
                )
            normalized["base_from_task"] = base_from_task
        tasks.append(normalized)
        seen.add(task_id)
    return tasks


def build_campaign(
    arguments: Mapping[str, Any],
    context: RepoContext,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes] | None]:
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
            "spec",
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
    if mode == "replay" and arguments.get("spec") is not None:
        raise InputError("spec is only valid for delivery campaigns")
    spec, spec_files = _openspec_reference(arguments.get("spec"), context)
    risk = validate_risk(arguments.get("risk", "high"))

    now = utc_now()
    campaign_id = new_campaign_id()
    tasks = _tasks(arguments.get("tasks"))
    task_definitions: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if task["kind"] != "implementation":
            task_definitions[task["id"]] = task
            continue
        task_context = (
            resolve_repo(task["workspace"])
            if "workspace" in task
            else context
        )
        task["workspace"] = str(task_context.repo_root)
        task["repository_key"] = str(task_context.git_common_dir)
        base_from_task = task.get("base_from_task")
        if isinstance(base_from_task, str):
            predecessor = task_definitions.get(base_from_task)
            if not isinstance(predecessor, dict) or predecessor.get(
                "kind"
            ) != "implementation":
                raise InputError("base_from_task must reference an implementation task")
            if predecessor.get("repository_key") != task["repository_key"]:
                raise InputError("base_from_task must use the same repository")
        else:
            task.setdefault("base_sha", task_context.head_sha)
        task_definitions[task["id"]] = task

    implementation_count = sum(
        task["kind"] == "implementation" and task.get("role") != "finalizer"
        for task in tasks
    )
    if mode == "delivery" and spec is None and (
        risk == "high"
        or implementation_count > 1
        or any(task.get("role") == "finalizer" for task in tasks)
    ):
        raise InputError(
            "an approved OpenSpec change is required for high-risk or multi-task "
            "delivery campaigns"
        )

    initial_worktree = status_snapshot(context)
    if initial_worktree["dirty"]:
        if spec is None:
            paths = ", ".join(
                str(path) for path in initial_worktree["paths"][:8]
            )
            suffix = " ..." if len(initial_worktree["paths"]) > 8 else ""
            raise InputError(
                "campaign workspace is dirty; use an isolated worktree before "
                f"creating the campaign ({paths}{suffix})"
            )
        if spec.get("storage") != "repository":
            paths = ", ".join(
                str(path) for path in initial_worktree["paths"][:8]
            )
            suffix = " ..." if len(initial_worktree["paths"]) > 8 else ""
            raise InputError(
                "campaign workspace is dirty; use an isolated worktree before "
                f"creating the campaign ({paths}{suffix})"
            )
        spec_prefix = f"{spec['path']}/"
        unrelated = [
            str(path)
            for path in initial_worktree["paths"]
            if not str(path).startswith(spec_prefix)
        ]
        if unrelated:
            paths = ", ".join(unrelated[:8])
            suffix = " ..." if len(unrelated) > 8 else ""
            raise InputError(
                "campaign workspace is dirty outside the frozen OpenSpec "
                f"change ({paths}{suffix})"
            )
    contract = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "created_at": now,
        "workspace": str(context.workspace),
        "repo_root": str(context.repo_root),
        "git_dir": str(context.git_dir),
        "git_common_dir": str(context.git_common_dir),
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
        "risk": risk,
        "mode": mode,
        "source": _source(arguments.get("source")),
        "spec": spec,
        "cutoff_at": cutoff_at,
        "withheld_evidence": REPLAY_WITHHELD_EVIDENCE if mode == "replay" else [],
        "tasks": tasks,
        "integration_policy": "combined-review-required",
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
        "task_transition_counts": {},
        "interventions": {},
        "provider_circuits": {
            "anthropic": {
                "status": "closed",
                "cooldown_until": None,
                "probe": None,
                "limit_count": 0,
                "updated_at": now,
            }
        },
        "runtime_versions": [],
        "candidate": None,
        "terminal": None,
    }
    return contract, state, spec_files


class CampaignStore:
    """Campaign storage alongside runs without exposing it in the worktree."""

    def __init__(self, context: RepoContext) -> None:
        self.context = context
        self.root = context.git_common_dir / "codex-agent-harness" / "campaigns"
        legacy_root = context.git_dir / "codex-agent-harness" / "campaigns"
        self.legacy_root = legacy_root if legacy_root != self.root else None

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
        root = self.root
        if self.legacy_root is not None and not (root / campaign_id).exists():
            legacy = self.legacy_root / campaign_id
            if legacy.is_dir():
                root = self.legacy_root
        path = root / campaign_id
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise StateError("campaign path escaped the state root") from exc
        return path

    def spec_dir(self, campaign_id: str) -> Path:
        return self.campaign_dir(campaign_id) / "spec"

    @staticmethod
    def _create_bytes(path: Path, content: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)

    def _create_spec(self, campaign_id: str, files: Mapping[str, bytes]) -> None:
        root = self.spec_dir(campaign_id)
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        for relative, content in sorted(files.items()):
            target = root / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent = target.parent
            while parent != root.parent:
                os.chmod(parent, 0o700)
                if parent == root:
                    break
                parent = parent.parent
            self._create_bytes(target, content)

    def create(
        self,
        contract: dict[str, Any],
        state: dict[str, Any],
        spec_files: Mapping[str, bytes] | None = None,
    ) -> None:
        self._ensure_root()
        campaign_id = str(contract["campaign_id"])
        directory = self.campaign_dir(campaign_id)
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise StateError("campaign_id already exists") from exc
        os.chmod(directory, 0o700)
        if spec_files is not None:
            self._create_spec(campaign_id, spec_files)
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
        roots = [self.root]
        if self.legacy_root is not None:
            roots.append(self.legacy_root)
        return sorted(
            {
                child.name
                for root in roots
                if root.is_dir()
                for child in root.iterdir()
                if child.is_dir() and CAMPAIGN_ID_RE.fullmatch(child.name)
            },
            reverse=True,
        )
