"""Permission-safe durable storage under a worktree's absolute Git directory."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .git_repo import RepoContext, resolve_repo
from .util import RUN_ID_RE, StateError, json_copy, utc_now


SCHEMA_VERSION = 1


class RunStore:
    def __init__(self, context: RepoContext) -> None:
        self.context = context
        self.root = context.git_dir / "codex-agent-harness" / "runs"

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> "RunStore":
        return cls(resolve_repo(workspace))

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root.parent, 0o700)
        os.chmod(self.root, 0o700)

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise StateError("invalid run_id")
        path = self.root / run_id
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError as exc:
            raise StateError("run path escaped the state root") from exc
        return path

    @staticmethod
    def _encoded(value: Any) -> bytes:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def _create_json(cls, path: Path, value: Any) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(cls._encoded(value))
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)

    @classmethod
    def _atomic_json(cls, path: Path, value: Any) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent)
        )
        temporary_path = Path(temporary)
        try:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(cls._encoded(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary_path, path)
            os.chmod(path, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"cannot read valid state from {path.name}") from exc
        if not isinstance(value, dict):
            raise StateError(f"{path.name} must contain a JSON object")
        return value

    def create(
        self,
        contract: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        self._ensure_root()
        directory = self.run_dir(str(contract["run_id"]))
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise StateError("run_id already exists") from exc
        os.chmod(directory, 0o700)
        self._create_json(directory / "contract.json", contract)
        self._create_json(directory / "state.json", state)
        self._create_json(
            directory / "review.json",
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": contract["run_id"],
                "review": None,
                "resolutions": {},
                "history": [],
            },
        )
        descriptor = os.open(
            directory / "events.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(descriptor)
        self.append_event(
            str(contract["run_id"]),
            {"type": "run_created", "phase": state["phase"]},
        )

    def read_contract(self, run_id: str) -> dict[str, Any]:
        value = self._read_json(self.run_dir(run_id) / "contract.json")
        if value.get("run_id") != run_id:
            raise StateError("contract run_id mismatch")
        return value

    def read_state(self, run_id: str) -> dict[str, Any]:
        value = self._read_json(self.run_dir(run_id) / "state.json")
        if value.get("run_id") != run_id:
            raise StateError("state run_id mismatch")
        if value.get("schema_version") != SCHEMA_VERSION:
            raise StateError("unsupported state schema_version")
        return value

    def save_state(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
        current = json_copy(state)
        current["revision"] = int(current.get("revision", 0)) + 1
        current["updated_at"] = utc_now()
        self._atomic_json(self.run_dir(run_id) / "state.json", current)
        return current

    def read_review(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "review.json")

    def save_review(self, run_id: str, review: dict[str, Any]) -> None:
        self._atomic_json(self.run_dir(run_id) / "review.json", review)

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "at": utc_now(),
            **json_copy(event),
        }
        path = self.run_dir(run_id) / "events.jsonl"
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

    def list_run_ids(self) -> Iterable[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            (
                child.name
                for child in self.root.iterdir()
                if child.is_dir() and RUN_ID_RE.fullmatch(child.name)
            ),
            reverse=True,
        )
