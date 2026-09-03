"""Read-only Git discovery and deterministic worktree fingerprints."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .util import InputError, unique_in_order


SHA_RE = re.compile(r"^[a-fA-F0-9]{7,64}$")

DIFF_CATEGORIES = (
    "production",
    "tests",
    "documentation",
    "configuration",
    "generated",
    "binary",
)
TEST_DIRECTORY_NAMES = {"test", "tests", "spec", "specs", "__tests__", "testdata"}
GENERATED_DIRECTORY_NAMES = {
    "build",
    "dist",
    "gen",
    "generated",
    "node_modules",
    "vendor",
}
DOCUMENTATION_DIRECTORY_NAMES = {
    "docs",
    "documentation",
    "openspec",
}
CONFIGURATION_DIRECTORY_NAMES = {
    ".claude",
    ".codex",
    ".github",
    ".gitlab",
    ".idea",
    ".vscode",
    "config",
    "configs",
    "configuration",
}
GENERATED_BASENAMES = {
    "cargo.lock",
    "composer.lock",
    "go.sum",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
}
DOCUMENTATION_BASENAMES = {
    "authors",
    "changelog",
    "contributing",
    "copying",
    "license",
    "readme",
}
CONFIGURATION_BASENAMES = {
    ".dockerignore",
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    ".npmrc",
    ".prettierrc",
    "codeowners",
    "dockerfile",
    "gemfile",
    "go.mod",
    "go.work",
    "jenkinsfile",
    "makefile",
    "pom.xml",
    "pyproject.toml",
    "rakefile",
    "requirements.txt",
}
DOCUMENTATION_SUFFIXES = {
    ".adoc",
    ".md",
    ".rst",
    ".txt",
}
CONFIGURATION_SUFFIXES = {
    ".cfg",
    ".conf",
    ".env",
    ".ini",
    ".json",
    ".lock",
    ".properties",
    ".tf",
    ".tfvars",
    ".toml",
    ".xml",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class RepoContext:
    workspace: Path
    repo_root: Path
    git_dir: Path
    git_common_dir: Path
    head_sha: str


def _git_env() -> dict[str, str]:
    environ = dict(os.environ)
    environ["GIT_OPTIONAL_LOCKS"] = "0"
    environ["LC_ALL"] = "C"
    return environ


def run_git(
    workspace: Path,
    arguments: Sequence[str],
    *,
    text: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=str(workspace),
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InputError(f"Git command failed before completion: {exc}") from exc


def _git_text(workspace: Path, arguments: Sequence[str]) -> str:
    completed = run_git(workspace, arguments, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise InputError(f"workspace is not a usable Git checkout{suffix}")
    return (completed.stdout or "").strip()


def resolve_repo(workspace: str | Path) -> RepoContext:
    candidate = Path(workspace).expanduser().resolve()
    if not candidate.is_dir():
        raise InputError("workspace must be an existing directory")
    repo_root = Path(
        _git_text(candidate, ["rev-parse", "--show-toplevel"])
    ).resolve()
    git_dir = Path(
        _git_text(candidate, ["rev-parse", "--absolute-git-dir"])
    ).resolve()
    git_common_dir = Path(
        _git_text(
            candidate,
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        )
    ).resolve()
    head_sha = _git_text(candidate, ["rev-parse", "HEAD"])
    return RepoContext(candidate, repo_root, git_dir, git_common_dir, head_sha)


def resolve_base_sha(context: RepoContext, value: str | None) -> str:
    """Resolve an explicit review base and require it to be an ancestor of HEAD."""

    if value is None:
        return context.head_sha
    if not SHA_RE.fullmatch(value):
        raise InputError("base_sha is invalid")
    resolved = run_git(
        context.repo_root,
        ["rev-parse", "--verify", f"{value}^{{commit}}"],
        text=True,
    )
    if resolved.returncode != 0:
        raise InputError("base_sha does not identify a local commit")
    base_sha = (resolved.stdout or "").strip().lower()
    ancestor = run_git(
        context.repo_root,
        ["merge-base", "--is-ancestor", base_sha, context.head_sha],
        text=True,
    )
    if ancestor.returncode != 0:
        raise InputError("base_sha must be an ancestor of HEAD")
    return base_sha


def _status_records(context: RepoContext) -> list[tuple[str, str]]:
    completed = run_git(
        context.repo_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        text=False,
    )
    if completed.returncode != 0:
        raise InputError("unable to inspect Git status")
    raw = completed.stdout or b""
    fields = raw.split(b"\0")
    records: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        decoded = os.fsdecode(field)
        if len(decoded) < 4:
            continue
        status = decoded[:2]
        path = decoded[3:]
        records.append((status, path))
        if ("R" in status or "C" in status) and index < len(fields):
            original = fields[index]
            index += 1
            if original:
                records.append((status, os.fsdecode(original)))
    return records


def status_snapshot(context: RepoContext) -> dict[str, object]:
    records = _status_records(context)
    paths = unique_in_order(path for _status, path in records)
    return {
        "dirty": bool(records),
        "paths": paths,
        "entries": [
            {"status": status, "path": path} for status, path in records
        ],
    }


def changed_paths(context: RepoContext) -> list[str]:
    return unique_in_order(path for _status, path in _status_records(context))


def _hash_untracked_file(digest: "hashlib._Hash", path: Path) -> None:
    try:
        stat = path.lstat()
    except OSError:
        digest.update(b"missing\0")
        return
    digest.update(str(stat.st_mode).encode("ascii"))
    digest.update(b"\0")
    if path.is_symlink():
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        return
    if not path.is_file():
        digest.update(b"non-file")
        return
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)


def diff_fingerprint(
    context: RepoContext,
    *,
    base_sha: str | None = None,
) -> tuple[str, list[str]]:
    base = base_sha or context.head_sha
    completed = run_git(
        context.repo_root,
        ["diff", "--binary", "--no-ext-diff", base, "--"],
        text=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise InputError("unable to calculate the Git diff fingerprint")

    names = run_git(
        context.repo_root,
        ["diff", "--name-only", "-z", base, "--"],
        text=False,
        timeout=60,
    )
    if names.returncode != 0:
        raise InputError("unable to calculate the changed Git paths")

    records = _status_records(context)
    diff_paths = [
        os.fsdecode(path)
        for path in (names.stdout or b"").split(b"\0")
        if path
    ]
    paths = unique_in_order(
        [*diff_paths, *(path for _status, path in records)]
    )
    digest = hashlib.sha256()
    digest.update(b"agent-harness-diff-v1\0")
    digest.update(base.encode("ascii", errors="strict"))
    digest.update(b"\0")
    digest.update(completed.stdout or b"")

    for status, relative in sorted(records, key=lambda item: (item[1], item[0])):
        digest.update(status.encode("ascii", errors="replace"))
        digest.update(b"\0")
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        if status == "??":
            _hash_untracked_file(digest, context.repo_root / relative)
            digest.update(b"\0")
    return digest.hexdigest(), paths


def _parse_numstat(raw: bytes) -> list[tuple[int | None, int | None, str]]:
    fields = raw.split(b"\0")
    records: list[tuple[int | None, int | None, str]] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        parts = field.split(b"\t", 2)
        if len(parts) != 3:
            raise InputError("unable to parse Git numstat output")
        added_raw, deleted_raw, path_raw = parts
        if not path_raw:
            if index + 1 >= len(fields):
                raise InputError("unable to parse renamed Git path")
            index += 1  # The old path is evidence only; classify the resulting path.
            path_raw = fields[index]
            index += 1
        added = int(added_raw) if added_raw != b"-" else None
        deleted = int(deleted_raw) if deleted_raw != b"-" else None
        records.append((added, deleted, os.fsdecode(path_raw)))
    return records


def _generated_attribute_paths(
    context: RepoContext,
    paths: Iterable[str],
    *,
    source: str | None = None,
) -> set[str]:
    unique = unique_in_order(paths)
    if not unique:
        return set()
    arguments = ["git", "check-attr", "-z"]
    if source is not None:
        arguments.extend(["--source", source])
    arguments.extend(["--stdin", "linguist-generated"])
    payload = b"".join(os.fsencode(path) + b"\0" for path in unique)
    try:
        completed = subprocess.run(
            arguments,
            cwd=str(context.repo_root),
            env=_git_env(),
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if completed.returncode != 0:
        return set()
    fields = (completed.stdout or b"").split(b"\0")
    generated: set[str] = set()
    for index in range(0, len(fields) - 2, 3):
        path_raw, attribute_raw, value_raw = fields[index : index + 3]
        if attribute_raw != b"linguist-generated":
            continue
        if value_raw in {b"set", b"true", b"yes", b"on", b"1"}:
            generated.add(os.fsdecode(path_raw))
    return generated


def _base_path_destinations(
    context: RepoContext,
    base_sha: str,
) -> dict[str, str]:
    """Map paths from the base tree to their result path for deletions and renames."""

    completed = run_git(
        context.repo_root,
        ["diff", "--name-status", "-z", "--no-ext-diff", base_sha, "--"],
        text=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise InputError("unable to inspect Git diff path statuses")
    fields = (completed.stdout or b"").split(b"\0")
    destinations: dict[str, str] = {}
    index = 0
    while index < len(fields):
        status_raw = fields[index]
        index += 1
        if not status_raw:
            continue
        status = os.fsdecode(status_raw)
        if index >= len(fields):
            raise InputError("unable to parse Git diff path statuses")
        original = os.fsdecode(fields[index])
        index += 1
        if status.startswith(("R", "C")):
            if index >= len(fields):
                raise InputError("unable to parse renamed Git path status")
            result = os.fsdecode(fields[index])
            index += 1
            destinations[original] = result
        elif status.startswith("D"):
            destinations[original] = original
    return destinations


def _looks_like_test(path: str) -> bool:
    candidate = Path(path)
    lowered_parts = {part.lower() for part in candidate.parts[:-1]}
    if lowered_parts & TEST_DIRECTORY_NAMES:
        return True
    name = candidate.name.lower()
    stem = candidate.stem.lower()
    return (
        name.startswith("test_")
        or stem.endswith("_test")
        or "_test." in name
        or ".test." in name
        or ".spec." in name
        or candidate.stem.endswith("Test")
        or candidate.stem.endswith("Tests")
    )


def _classify_text_path(path: str, generated_attributes: set[str]) -> str:
    candidate = Path(path)
    parts = {part.lower() for part in candidate.parts[:-1]}
    name = candidate.name.lower()
    suffix = candidate.suffix.lower()
    if (
        path in generated_attributes
        or parts & GENERATED_DIRECTORY_NAMES
        or name in GENERATED_BASENAMES
        or ".generated." in name
        or name.endswith(
            (
                ".pb.c",
                ".pb.cc",
                ".pb.cpp",
                ".pb.cs",
                ".pb.go",
                ".pb.h",
                ".g.cs",
                ".g.dart",
                ".g.java",
                ".freezed.dart",
                ".snap",
            )
        )
        or "__snapshots__" in parts
    ):
        return "generated"
    if _looks_like_test(path):
        return "tests"
    basename_without_suffix = name.split(".", 1)[0]
    if (
        parts & DOCUMENTATION_DIRECTORY_NAMES
        or name in DOCUMENTATION_BASENAMES
        or basename_without_suffix in DOCUMENTATION_BASENAMES
        or suffix in DOCUMENTATION_SUFFIXES
    ):
        return "documentation"
    if (
        parts & CONFIGURATION_DIRECTORY_NAMES
        or name in CONFIGURATION_BASENAMES
        or basename_without_suffix in CONFIGURATION_BASENAMES
        or suffix in CONFIGURATION_SUFFIXES
        or name.startswith(".env")
    ):
        return "configuration"
    return "production"


def _untracked_numstat(path: Path) -> tuple[int | None, int | None]:
    try:
        if path.is_symlink() or not path.is_file():
            return None, None
        line_count = 0
        saw_content = False
        ended_with_newline = True
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                if b"\0" in chunk:
                    return None, None
                saw_content = True
                line_count += chunk.count(b"\n")
                ended_with_newline = chunk.endswith(b"\n")
        if saw_content and not ended_with_newline:
            line_count += 1
        return line_count, 0
    except OSError as exc:
        raise InputError(f"unable to inspect untracked file: {path.name}") from exc


def diff_stats(
    context: RepoContext,
    *,
    base_sha: str | None = None,
) -> dict[str, Any]:
    """Return sanitized line counts for the current diff, including untracked files."""

    base = base_sha or context.head_sha
    completed = run_git(
        context.repo_root,
        ["diff", "--numstat", "-z", "--no-ext-diff", base, "--"],
        text=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise InputError("unable to calculate Git diff statistics")
    records = _parse_numstat(completed.stdout or b"")
    tracked_paths = {path for _added, _deleted, path in records}
    for status, relative in _status_records(context):
        if status == "??" and relative not in tracked_paths:
            added, deleted = _untracked_numstat(context.repo_root / relative)
            records.append((added, deleted, relative))

    generated_attributes = _generated_attribute_paths(
        context, (path for _added, _deleted, path in records)
    )
    base_destinations = _base_path_destinations(context, base)
    base_generated = _generated_attribute_paths(
        context,
        base_destinations,
        source=base,
    )
    generated_attributes.update(
        base_destinations[path] for path in base_generated
    )
    totals = {
        category: {"added": 0, "deleted": 0, "total": 0, "files": 0}
        for category in DIFF_CATEGORIES
    }
    path_stats: list[dict[str, Any]] = []
    for added, deleted, path in sorted(records, key=lambda item: item[2]):
        category = (
            "binary"
            if added is None or deleted is None
            else _classify_text_path(path, generated_attributes)
        )
        entry = {
            "path": path,
            "category": category,
            "added": added,
            "deleted": deleted,
            "total": None if added is None or deleted is None else added + deleted,
        }
        path_stats.append(entry)
        totals[category]["files"] += 1
        if added is not None and deleted is not None:
            totals[category]["added"] += added
            totals[category]["deleted"] += deleted
            totals[category]["total"] += added + deleted
    return {**totals, "paths": path_stats}
