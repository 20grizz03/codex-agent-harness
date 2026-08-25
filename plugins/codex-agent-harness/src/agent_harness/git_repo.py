"""Read-only Git discovery and deterministic worktree fingerprints."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .util import InputError, unique_in_order


SHA_RE = re.compile(r"^[a-fA-F0-9]{7,64}$")


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
