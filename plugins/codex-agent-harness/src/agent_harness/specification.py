"""Local Markdown drafts and immutable, task-scoped approved specifications."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from .git_repo import RepoContext, run_git
from .util import InputError, StateError, require_string, sanitize_text


ROOT = ".agent-harness/specs"
CHANGE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
TASK_PATH_RE = re.compile(r"^tasks/[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\.md$")
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024


def is_native(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("kind") == "harness"


def _document_path(relative: str) -> bool:
    return relative == "spec.md" or bool(TASK_PATH_RE.fullmatch(relative))


def _read_document(root: Path, relative: str) -> bytes:
    if not _document_path(relative):
        raise InputError("invalid specification document path")
    target = root / relative
    if any(path.is_symlink() for path in (target, *target.parents)):
        raise InputError(f"specification paths must not contain symlinks: {relative}")
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise InputError(f"specification document must be a regular file: {relative}")
            content = stream.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise InputError(f"cannot read specification document: {relative}") from exc
    if len(content) > MAX_DOCUMENT_BYTES:
        raise InputError(f"specification document is too large: {relative}")
    return content


def _validate_draft(content: bytes, relative: str) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError(f"specification must be UTF-8 Markdown: {relative}") from exc
    if not text.strip() or "\x00" in text:
        raise InputError(f"specification document is empty or contains NUL: {relative}")
    # Не исправляем согласованный текст молча и не копируем известные секреты.
    if sanitize_text(text, maximum=len(text)) != text.strip() or re.search(
        r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|https?://[^\s/@]+:[^\s/@]+@", text
    ):
        raise InputError(f"specification contains possible credentials; remove them before approval: {relative}")


def _reference(change_id: str, readiness: str, files: Mapping[str, bytes]) -> dict:
    reference = {
        "kind": "harness",
        "schema_version": 1,
        "change_id": change_id,
        "storage": "local",
        "path": f"{ROOT}/{change_id}",
        "readiness": readiness,
        "documents": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(files.items())
        },
    }
    reference["sha256"] = hashlib.sha256(
        json.dumps(reference, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return reference


def prepare_spec(
    value: Any, context: RepoContext, task_ids: list[str] | None = None,
) -> tuple[dict, dict[str, bytes]]:
    if not is_native(value) or set(value) - {"kind", "change_id", "storage", "readiness"}:
        raise InputError("native spec requires kind, change_id, readiness and optional local storage")
    change_id = require_string(value.get("change_id"), "spec.change_id", maximum=128)
    if not CHANGE_RE.fullmatch(change_id):
        raise InputError("spec.change_id must be lowercase kebab-case")
    if value.get("storage", "local") != "local":
        raise InputError("native specifications use local storage only")
    readiness = value.get("readiness")
    if readiness not in ("ready", "analysis_required"):
        raise InputError("spec.readiness must be ready or analysis_required")
    names = ["spec.md", *(f"tasks/{task_id}.md" for task_id in task_ids or [])]
    if len(names) > 65 or len({name.casefold() for name in names}) != len(names):
        raise InputError("invalid specification task set")
    root = context.repo_root / ROOT / change_id
    files = {}
    for name in names:
        content = _read_document(root, name)
        _validate_draft(content, name)
        relative = (root / name).relative_to(context.repo_root).as_posix()
        ignored = run_git(context.repo_root, ["check-ignore", "--quiet", "--", relative])
        if ignored.returncode != 0:
            raise InputError("native specification must be untracked and ignored; add /.agent-harness/specs/ to .git/info/exclude")
        files[name] = content
        if sum(map(len, files.values())) > MAX_TOTAL_BYTES:
            raise InputError("specification is too large")
    return _reference(change_id, readiness, files), files


def verify_snapshot(reference: Any, root: Path | None) -> dict[str, bytes]:
    try:
        if not is_native(reference) or root is None:
            raise InputError("native specification snapshot is unavailable")
        change_id = require_string(reference.get("change_id"), "spec.change_id", maximum=128)
        documents = reference.get("documents")
        if (
            not CHANGE_RE.fullmatch(change_id)
            or type(reference.get("schema_version")) is not int
            or reference.get("readiness") not in ("ready", "analysis_required")
            or not isinstance(documents, dict)
            or not 1 <= len(documents) <= 65
            or "spec.md" not in documents
            or not all(isinstance(name, str) for name in documents)
        ):
            raise InputError("invalid stored specification reference")
        files = {}
        for name in documents:
            files[name] = _read_document(root, name)
            if sum(map(len, files.values())) > MAX_TOTAL_BYTES:
                raise InputError("specification is too large")
        if reference != _reference(change_id, reference["readiness"], files):
            raise InputError("approved specification fingerprint mismatch")
        return files
    except (InputError, OSError) as exc:
        raise StateError(f"cannot verify approved native specification: {exc}") from exc


def task_snapshot(reference: Mapping, root: Path, task_id: str) -> tuple[dict, dict]:
    files = verify_snapshot(reference, root)
    name = f"tasks/{task_id}.md"
    if name not in files:
        raise StateError("approved specification has no document for this task")
    scoped = {"spec.md": files["spec.md"], name: files[name]}
    return _reference(reference["change_id"], reference["readiness"], scoped), scoped


def write_snapshot(root: Path, files: Mapping[str, bytes]) -> None:
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    for name, content in files.items():
        if not _document_path(name):
            raise InputError("invalid specification document path")
        target = root / name
        target.parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(target.parent, 0o700)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())


def contract_references(reference: Mapping) -> list[dict[str, str]]:
    return [
        {"ref": f"harness:{reference['change_id']}/{name}", "revision": digest}
        for name, digest in reference["documents"].items()
    ]


def public_context(reference: Any, root: Path) -> dict | None:
    if not is_native(reference):
        return None
    verify_snapshot(reference, root)
    return {
        "kind": "harness",
        "revision": reference["sha256"],
        "readiness": reference["readiness"],
        "authority": "approved_snapshot",
        "documents": {name: str(root / name) for name in reference["documents"]},
    }
