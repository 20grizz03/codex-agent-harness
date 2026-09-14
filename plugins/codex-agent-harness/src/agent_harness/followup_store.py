"""Atomic, worktree-shared persistence for bounded review follow-ups."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .git_repo import RepoContext, resolve_repo
from .util import InputError, StateError, json_copy, utc_now


SCHEMA_VERSION = 1
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
DIGEST_RE = re.compile(r"[a-f0-9]{64}\Z")


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise InputError(f"invalid {name}")
    return value


def _revision(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise InputError(f"{name} must be a non-negative integer")
    return value


def _json_value(value: Any, name: str) -> Any:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded, object_pairs_hook=_unique_object)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InputError(f"{name} must be valid JSON") from exc


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


class FollowupStore:
    """Serialize all follow-up transactions through one common-Git-dir lock."""

    def __init__(self, context: RepoContext) -> None:
        self.context = context
        self.root = context.git_common_dir / "codex-agent-harness" / "followups"

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> "FollowupStore":
        return cls(resolve_repo(workspace))

    @staticmethod
    def _directory(
        path: Path, *, create: bool, enforce_permissions: bool = True
    ) -> None:
        created = False
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not create:
                raise StateError("follow-up directory is missing") from None
            try:
                path.mkdir(mode=0o700)
                created = True
            except FileExistsError:
                pass
            info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise StateError("follow-up path must be a real directory")
        if enforce_permissions:
            os.chmod(path, 0o700)
        if created:
            FollowupStore._fsync_dir(path.parent)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _ensure_root(self) -> None:
        common = self.context.git_common_dir
        if not stat.S_ISDIR(common.lstat().st_mode):
            raise StateError("Git common directory must be real")
        self._directory(self.root.parent, create=True)
        self._directory(self.root, create=True)

    def _check_root(self) -> None:
        common = self.context.git_common_dir
        if not stat.S_ISDIR(common.lstat().st_mode):
            raise StateError("Git common directory must be real")
        self._directory(self.root.parent, create=False, enforce_permissions=False)
        self._directory(self.root, create=False, enforce_permissions=False)

    @contextmanager
    def _locked(self, *, read_only: bool = False) -> Iterator[None]:
        if read_only:
            self._check_root()
        else:
            self._ensure_root()
        lock_path = self.root / ".lock"
        flags = (os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise StateError("cannot open follow-up lock") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StateError("follow-up lock must be a regular file")
            if not read_only:
                os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_SH if read_only else fcntl.LOCK_EX)
            try:
                # The pathname must still identify the inode we locked.
                try:
                    path_info = lock_path.lstat()
                except OSError as exc:
                    raise StateError("follow-up lock changed while opening") from exc
                if (
                    not stat.S_ISREG(path_info.st_mode)
                    or path_info.st_ino != info.st_ino
                    or path_info.st_dev != info.st_dev
                ):
                    raise StateError("follow-up lock changed while opening")
                if read_only:
                    self._check_root()
                else:
                    self._ensure_root()
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _journal_path(
        self, followup_id: str, *, create: bool, read_only: bool = False
    ) -> Path:
        _identifier(followup_id, "followup_id")
        directory = self.root / followup_id
        # The identifier contains no separators, and every directory component
        # is checked without following symlinks before file access.
        self._directory(
            directory, create=create, enforce_permissions=not read_only
        )
        return directory / "journal.json"

    @staticmethod
    def _regular_file(path: Path) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StateError("follow-up journal must be a regular file")
        return True

    @classmethod
    def _read_journal(cls, path: Path, followup_id: str) -> dict[str, Any]:
        if not cls._regular_file(path):
            raise StateError("follow-up journal is missing")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                document = json.load(
                    stream,
                    object_pairs_hook=_unique_object,
                    parse_constant=_invalid_constant,
                )
        except (OSError, UnicodeError, ValueError) as exc:
            raise StateError("cannot read valid follow-up journal") from exc
        cls._validate_journal(document, followup_id)
        return document

    @staticmethod
    def _validate_journal(document: Any, followup_id: str) -> None:
        if not isinstance(document, dict) or set(document) != {
            "schema_version", "contract", "state", "receipts", "creation_digest",
            "contract_digest",
        }:
            raise StateError("invalid follow-up journal structure")
        if (
            type(document["schema_version"]) is not int
            or document["schema_version"] != SCHEMA_VERSION
        ):
            raise StateError("unsupported follow-up journal schema_version")
        contract = document["contract"]
        state = document["state"]
        receipts = document["receipts"]
        if not isinstance(contract, dict) or contract.get("followup_id") != followup_id:
            raise StateError("follow-up contract identity mismatch")
        try:
            _identifier(contract.get("owner_id"), "owner_id")
        except InputError as exc:
            raise StateError("invalid persisted follow-up owner_id") from exc
        if not isinstance(state, dict):
            raise StateError("follow-up state must be an object")
        if (
            type(state.get("schema_version")) is not int
            or state["schema_version"] != SCHEMA_VERSION
        ):
            raise StateError("unsupported follow-up state schema_version")
        if type(state.get("revision")) is not int or state["revision"] < 0:
            raise StateError("invalid follow-up state revision")
        if state.get("followup_id", followup_id) != followup_id:
            raise StateError("follow-up state identity mismatch")
        if state.get("owner_id", contract["owner_id"]) != contract["owner_id"]:
            raise StateError("follow-up state owner mismatch")
        FollowupStore._cancelled(state)
        if (
            not isinstance(document["creation_digest"], str)
            or not DIGEST_RE.fullmatch(document["creation_digest"])
        ):
            raise StateError("invalid follow-up creation digest")
        if (
            not isinstance(document["contract_digest"], str)
            or not DIGEST_RE.fullmatch(document["contract_digest"])
            or document["contract_digest"] != _digest(contract)
        ):
            raise StateError("follow-up contract is corrupt")
        if not isinstance(receipts, dict):
            raise StateError("invalid follow-up receipts")
        for request_id, receipt in receipts.items():
            if (
                not ID_RE.fullmatch(request_id)
                or not isinstance(receipt, dict)
                or set(receipt) != {"owner_id", "payload_sha256"}
            ):
                raise StateError("invalid follow-up receipt")
            if (
                receipt["owner_id"] != contract["owner_id"]
                or not isinstance(receipt["payload_sha256"], str)
                or not DIGEST_RE.fullmatch(receipt["payload_sha256"])
            ):
                raise StateError("invalid follow-up receipt")
        if len(receipts) != state["revision"]:
            raise StateError("follow-up receipt count does not match revision")
        if (
            state["revision"] == 0
            and not receipts
            and document["creation_digest"]
            != _digest({"contract": contract, "state": state})
        ):
            raise StateError("follow-up creation input is corrupt")

    @staticmethod
    def _atomic_write(path: Path, document: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".journal.", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                encoded = json.dumps(
                    document,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                stream.write((encoded + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.close(descriptor)
            descriptor = -1
            FollowupStore._regular_file(path)
            os.replace(temporary, path)
            FollowupStore._fsync_dir(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _public(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            "contract": json_copy(document["contract"]),
            "state": json_copy(document["state"]),
        }

    @staticmethod
    def _cancelled(state: dict[str, Any]) -> bool:
        if "terminal" not in state:
            return False
        terminal = state["terminal"]
        if (
            not isinstance(terminal, dict)
            or set(terminal) != {"status", "summary"}
            or terminal["status"] != "cancelled"
            or not isinstance(terminal["summary"], str)
        ):
            raise StateError("invalid follow-up terminal")
        return True

    @staticmethod
    def _active_resources(document: dict[str, Any]) -> set[str]:
        contract, state = document["contract"], document["state"]
        cancelled = FollowupStore._cancelled(state)
        tasks = contract.get("tasks")
        if tasks is None:
            return set()
        current = state.get("tasks")
        if (
            not isinstance(tasks, dict)
            or not isinstance(current, dict)
            or set(tasks) != set(current)
            or not isinstance(state.get("queued_sources"), list)
        ):
            raise StateError("invalid follow-up task resources")
        active = bool(state["queued_sources"])
        for task_state in current.values():
            if not isinstance(task_state, dict) or not isinstance(
                task_state.get("status"), str
            ):
                raise StateError("invalid follow-up task status")
            active |= task_state["status"] not in {
                "mechanical_checked", "complete"
            }
        if cancelled or not active:
            return set()

        def resource(value: Any) -> str:
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise StateError("invalid follow-up worktree resource")
            try:
                return str(Path(value).resolve())
            except (OSError, RuntimeError) as exc:
                raise StateError("invalid follow-up worktree resource") from exc

        resources: set[str] = set()
        for task_id, task in tasks.items():
            if not isinstance(task, dict):
                raise StateError("invalid follow-up task resource")
            resources.add(resource(task.get("workspace")))
            task_state = current[task_id]
            if "workspace" in task_state:
                resources.add(resource(task_state["workspace"]))
            for field in ("candidate", "checkpoint_baseline"):
                nested = task_state.get(field)
                if nested is None:
                    continue
                if not isinstance(nested, dict):
                    raise StateError("invalid follow-up task resource")
                resources.add(resource(nested.get("workspace")))
        return resources

    def _reject_overlap(
        self, followup_id: str, document: dict[str, Any]
    ) -> None:
        resources = self._active_resources(document)
        if not resources:
            return
        for directory in self.root.iterdir():
            if directory.name == followup_id or not ID_RE.fullmatch(directory.name):
                continue
            path = self._journal_path(directory.name, create=False)
            if not self._regular_file(path):
                continue  # A create interrupted before its first atomic replace.
            other = self._read_journal(path, directory.name)
            if resources & self._active_resources(other):
                raise StateError("another active follow-up reserves a worktree")

    def create(
        self, followup_id: str, contract: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        _identifier(followup_id, "followup_id")
        contract_copy = _json_value(contract, "contract")
        state_copy = _json_value(state, "state")
        if not isinstance(contract_copy, dict) or contract_copy.get("followup_id") != followup_id:
            raise InputError("contract followup_id mismatch")
        _identifier(contract_copy.get("owner_id"), "owner_id")
        if not isinstance(state_copy, dict):
            raise InputError("state must be an object")
        if (
            type(state_copy.get("schema_version")) is not int
            or state_copy["schema_version"] != SCHEMA_VERSION
        ):
            raise InputError("state schema_version must be 1")
        if _revision(state_copy.get("revision"), "state.revision") != 0:
            raise InputError("initial state revision must be 0")
        if state_copy.get("followup_id", followup_id) != followup_id:
            raise InputError("state followup_id mismatch")
        if state_copy.get("owner_id", contract_copy["owner_id"]) != contract_copy["owner_id"]:
            raise InputError("state owner_id mismatch")
        creation_digest = _digest({"contract": contract_copy, "state": state_copy})
        with self._locked():
            path = self._journal_path(followup_id, create=True)
            if self._regular_file(path):
                current = self._read_journal(path, followup_id)
                if (
                    current["contract"] != contract_copy
                    or current["creation_digest"] != creation_digest
                ):
                    raise StateError("follow-up already exists with different creation input")
                return self._public(current)
            document = {
                "schema_version": SCHEMA_VERSION,
                "contract": contract_copy,
                "state": state_copy,
                "receipts": {},
                "creation_digest": creation_digest,
                "contract_digest": _digest(contract_copy),
            }
            self._reject_overlap(followup_id, document)
            self._atomic_write(path, document)
            return self._public(document)

    def read(self, followup_id: str) -> dict[str, dict[str, Any]]:
        _identifier(followup_id, "followup_id")
        with self._locked(read_only=True):
            path = self._journal_path(followup_id, create=False, read_only=True)
            return self._public(self._read_journal(path, followup_id))

    def mutate(
        self,
        followup_id: str,
        *,
        owner_id: str,
        expected_revision: int,
        request_id: str,
        payload: Any,
        update: Callable[[dict[str, Any], dict[str, Any]], None],
    ) -> dict[str, dict[str, Any]]:
        _identifier(followup_id, "followup_id")
        _identifier(owner_id, "owner_id")
        _revision(expected_revision, "expected_revision")
        _identifier(request_id, "request_id")
        payload_digest = _digest(_json_value(payload, "payload"))
        if not callable(update):
            raise InputError("update must be callable")
        with self._locked():
            path = self._journal_path(followup_id, create=False)
            document = self._read_journal(path, followup_id)
            if document["contract"]["owner_id"] != owner_id:
                raise StateError("follow-up owner_id mismatch")
            prior = document["receipts"].get(request_id)
            if prior is not None:
                if prior != {"owner_id": owner_id, "payload_sha256": payload_digest}:
                    raise StateError("request_id reused with different payload or owner")
                return self._public(document)
            if document["state"]["revision"] != expected_revision:
                raise StateError("follow-up revision mismatch")
            contract = json_copy(document["contract"])
            state = json_copy(document["state"])
            result = update(contract, state)
            if result is not None:
                raise InputError("update must mutate state and return None")
            if contract != document["contract"]:
                raise StateError("follow-up contract is immutable")
            if not isinstance(state, dict):
                raise StateError("follow-up update removed state")
            if (
                state.get("schema_version") != SCHEMA_VERSION
                or type(state.get("schema_version")) is not int
            ):
                raise StateError("follow-up state schema_version is immutable")
            if (
                state.get("revision") != expected_revision
                or type(state.get("revision")) is not int
            ):
                raise StateError("follow-up update may not change revision")
            if (
                state.get("followup_id", followup_id) != followup_id
                or state.get("owner_id", owner_id) != owner_id
            ):
                raise StateError("follow-up state identity is immutable")
            for field in ("followup_id", "owner_id"):
                if (field in state) != (field in document["state"]):
                    raise StateError("follow-up state identity is immutable")
            state["revision"] = expected_revision + 1
            state["updated_at"] = utc_now()
            document["state"] = _json_value(state, "state")
            document["receipts"][request_id] = {
                "owner_id": owner_id,
                "payload_sha256": payload_digest,
            }
            self._reject_overlap(followup_id, document)
            self._atomic_write(path, document)
            return self._public(document)
