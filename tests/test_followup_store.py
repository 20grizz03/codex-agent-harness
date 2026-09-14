from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support

from agent_harness.followup_store import FollowupStore
from agent_harness.git_repo import resolve_repo
from agent_harness.util import InputError, StateError


FOLLOWUP_ID = "followup.1"
OWNER_ID = "run-owner_1"


def initial() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {"followup_id": FOLLOWUP_ID, "owner_id": OWNER_ID,
         "goal": "Fix review", "summary": "Original"},
        {"schema_version": 1, "revision": 0, "phase": "open", "history": []},
    )


def increment(_contract: dict[str, object], state: dict[str, object]) -> None:
    state["count"] = int(state.get("count", 0)) + 1


def process_mutate(
    workspace: str,
    ready: multiprocessing.synchronize.Event,
    request_id: str,
    payload: dict[str, object],
    output: multiprocessing.queues.Queue,
) -> None:
    ready.wait()
    try:
        result = FollowupStore.for_workspace(workspace).mutate(
            FOLLOWUP_ID,
            owner_id=OWNER_ID,
            expected_revision=0,
            request_id=request_id,
            payload=payload,
            update=increment,
        )
        output.put(("ok", result["state"]["revision"]))
    except (InputError, StateError) as exc:
        output.put(("error", str(exc)))


def resource_document(
    followup_id: str, owner_id: str, workspace: Path, source: Path
) -> tuple[dict, dict]:
    return (
        {"followup_id": followup_id, "owner_id": owner_id,
         "source": {"workspace": str(source)},
         "tasks": {"A": {"workspace": str(workspace)}}},
        {"schema_version": 1, "revision": 0,
         "tasks": {"A": {"status": "pending"}}, "queued_sources": []},
    )


def process_create_overlap(
    workspace: str,
    reserved: str,
    gate: multiprocessing.synchronize.Event,
    followup_id: str,
    output: multiprocessing.queues.Queue,
) -> None:
    gate.wait()
    try:
        contract, state = resource_document(
            followup_id, followup_id, Path(reserved), Path(workspace)
        )
        FollowupStore.for_workspace(workspace).create(
            followup_id, contract, state
        )
        output.put("ok")
    except (InputError, StateError) as exc:
        output.put(str(exc))


class FollowupStoreTests(unittest.TestCase):
    def test_read_missing_store_leaves_git_directory_unchanged(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            common = store.context.git_common_dir
            before = sorted(path.relative_to(common) for path in common.rglob("*"))

            with self.assertRaisesRegex(StateError, "directory is missing"):
                store.read(FOLLOWUP_ID)

            self.assertEqual(
                before, sorted(path.relative_to(common) for path in common.rglob("*"))
            )
            self.assertFalse(store.root.parent.exists())

    def test_read_never_repairs_permissions_or_creates_lock(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            store.create(FOLLOWUP_ID, *initial())
            directories = (store.root.parent, store.root, store.root / FOLLOWUP_ID)
            lock = store.root / ".lock"
            journal = store.root / FOLLOWUP_ID / "journal.json"
            original_modes = {
                path: stat.S_IMODE(path.stat().st_mode)
                for path in (*directories, lock, journal)
            }
            open_file = os.open

            def read_open(path: os.PathLike[str] | str, flags: int, *args: object) -> int:
                self.assertFalse(flags & (os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_RDWR))
                return open_file(path, flags, *args)

            try:
                for directory in directories:
                    directory.chmod(0o500)
                lock.chmod(0o400)
                journal.chmod(0o400)
                with (
                    mock.patch("agent_harness.followup_store.os.open", side_effect=read_open),
                    mock.patch("agent_harness.followup_store.os.chmod", side_effect=AssertionError("chmod on read")),
                    mock.patch("agent_harness.followup_store.os.fchmod", side_effect=AssertionError("fchmod on read")),
                    mock.patch("agent_harness.followup_store.fcntl.flock", wraps=fcntl.flock) as flock,
                ):
                    self.assertEqual(0, store.read(FOLLOWUP_ID)["state"]["revision"])
                self.assertEqual(fcntl.LOCK_SH, flock.call_args_list[0].args[1])
                for directory in directories:
                    self.assertEqual(0o500, stat.S_IMODE(directory.stat().st_mode))
                for file in (lock, journal):
                    self.assertEqual(0o400, stat.S_IMODE(file.stat().st_mode))
            finally:
                for path, mode in original_modes.items():
                    path.chmod(mode)

            journal.unlink()
            with self.assertRaisesRegex(StateError, "journal is missing"):
                store.read(FOLLOWUP_ID)
            self.assertFalse(journal.exists())

            lock.unlink()
            with self.assertRaisesRegex(StateError, "cannot open follow-up lock"):
                store.read(FOLLOWUP_ID)
            self.assertFalse(lock.exists())

    def test_mutate_still_repairs_existing_permissions(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            store.create(FOLLOWUP_ID, *initial())
            directories = (store.root.parent, store.root, store.root / FOLLOWUP_ID)
            lock = store.root / ".lock"
            journal = store.root / FOLLOWUP_ID / "journal.json"
            for directory in directories:
                directory.chmod(0o755)
            lock.chmod(0o644)
            journal.chmod(0o644)

            store.mutate(
                FOLLOWUP_ID, owner_id=OWNER_ID, expected_revision=0,
                request_id="repair-modes", payload={}, update=increment,
            )

            for directory in directories:
                self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
            for file in (lock, journal):
                self.assertEqual(0o600, stat.S_IMODE(file.stat().st_mode))

    def test_active_followups_reserve_task_worktrees_but_not_source(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            first = repo.path / "first"
            second = repo.path / "second"
            first.mkdir()
            second.mkdir()
            store.create("first-id", *resource_document(
                "first-id", "owner-a", first, repo.path
            ))
            with self.assertRaisesRegex(StateError, "reserves a worktree"):
                store.create("overlap-id", *resource_document(
                    "overlap-id", "owner-b", first, repo.path
                ))
            with self.assertRaisesRegex(StateError, "reserves a worktree"):
                store.create("same-owner-id", *resource_document(
                    "same-owner-id", "owner-a", first, repo.path
                ))
            store.create("independent-id", *resource_document(
                "independent-id", "owner-b", second, repo.path
            ))
            self.assertEqual(0, store.read("independent-id")["state"]["revision"])

            def complete(_contract: dict, state: dict) -> None:
                state["tasks"]["A"]["status"] = "mechanical_checked"

            store.mutate("first-id", owner_id="owner-a", expected_revision=0,
                         request_id="done", payload={}, update=complete)
            store.create("after-done", *resource_document(
                "after-done", "owner-c", first, repo.path
            ))

    def test_cancelled_followup_releases_resources_and_keeps_history(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            reserved = repo.path / "reserved"
            reserved.mkdir()
            contract, state = resource_document(
                "cancelled-id", "owner-a", reserved, repo.path
            )
            state["queued_sources"] = [{"head_sha": "new-parent"}]
            state["history"] = [{"event": "started"}]
            store.create("cancelled-id", contract, state)
            replacement = resource_document(
                "replacement-id", "owner-b", reserved, repo.path
            )
            with self.assertRaisesRegex(StateError, "reserves a worktree"):
                store.create("replacement-id", *replacement)

            def cancel(_contract: dict, current: dict) -> None:
                current["history"].append({"event": "cancelled"})
                current["terminal"] = {
                    "status": "cancelled", "summary": "No longer needed"
                }

            store.mutate(
                "cancelled-id", owner_id="owner-a", expected_revision=0,
                request_id="cancel", payload={}, update=cancel,
            )
            store.create("replacement-id", *replacement)
            old = store.read("cancelled-id")["state"]
            self.assertEqual(1, old["revision"])
            self.assertEqual(
                [{"event": "started"}, {"event": "cancelled"}], old["history"]
            )
            self.assertEqual("cancelled", old["terminal"]["status"])
            with self.assertRaisesRegex(StateError, "reserves a worktree"):
                store.create("third-id", *resource_document(
                    "third-id", "owner-c", reserved, repo.path
                ))

    def test_invalid_terminal_cannot_release_resources(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            reserved = repo.path / "reserved"
            reserved.mkdir()
            store.create("active-id", *resource_document(
                "active-id", "owner-a", reserved, repo.path
            ))
            invalid = (
                None,
                {},
                {"status": "complete", "summary": "wrong status"},
                {"status": "cancelled", "summary": 7},
                {"status": "cancelled", "summary": "ok", "extra": True},
            )
            for index, terminal in enumerate(invalid):
                with self.subTest(index=index):
                    def update(_contract: dict, state: dict) -> None:
                        state["terminal"] = terminal

                    with self.assertRaisesRegex(StateError, "invalid follow-up terminal"):
                        store.mutate(
                            "active-id", owner_id="owner-a", expected_revision=0,
                            request_id=f"invalid-{index}", payload={}, update=update,
                        )
                    self.assertEqual(0, store.read("active-id")["state"]["revision"])

            journal = store.root / "active-id" / "journal.json"
            damaged = json.loads(journal.read_text(encoding="utf-8"))
            damaged["state"]["terminal"] = {"status": "complete", "summary": "wrong"}
            journal.write_text(json.dumps(damaged), encoding="utf-8")
            with self.assertRaisesRegex(StateError, "invalid follow-up terminal"):
                store.read("active-id")

    def test_dynamic_candidate_and_checkpoint_resources_are_reserved(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            first = repo.path / "first"
            second = repo.path / "second"
            first.mkdir()
            second.mkdir()
            store.create("first-id", *resource_document(
                "first-id", "owner-a", first, repo.path
            ))
            store.create("second-id", *resource_document(
                "second-id", "owner-b", second, repo.path
            ))

            for field in ("workspace", "candidate", "checkpoint_baseline"):
                def overlap(_contract: dict, state: dict) -> None:
                    if field == "workspace":
                        state["tasks"]["A"][field] = str(second)
                    else:
                        state["tasks"]["A"][field] = {"workspace": str(second)}

                with self.subTest(field=field), self.assertRaisesRegex(
                    StateError, "reserves a worktree"
                ):
                    store.mutate(
                        "first-id", owner_id="owner-a", expected_revision=0,
                        request_id=f"overlap-{field}", payload={}, update=overlap,
                    )
            self.assertEqual(0, store.read("first-id")["state"]["revision"])

    def test_queued_source_keeps_completed_chain_active(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            reserved = repo.path / "reserved"
            reserved.mkdir()
            contract, state = resource_document(
                "queued-id", "owner-a", reserved, repo.path
            )
            state["tasks"]["A"]["status"] = "complete"
            state["queued_sources"] = [{"head_sha": "new-parent"}]
            store.create("queued-id", contract, state)
            with self.assertRaisesRegex(StateError, "reserves a worktree"):
                store.create("other-id", *resource_document(
                    "other-id", "owner-b", reserved, repo.path
                ))

    def test_concurrent_create_cannot_reserve_same_worktree_twice(self) -> None:
        context = multiprocessing.get_context("fork")
        with _support.TempRepo() as repo:
            reserved = repo.path / "reserved"
            reserved.mkdir()
            gate = context.Event()
            output = context.Queue()
            processes = [
                context.Process(
                    target=process_create_overlap,
                    args=(str(repo.path), str(reserved), gate, f"chain-{index}", output),
                )
                for index in range(2)
            ]
            for process in processes:
                process.start()
            gate.set()
            outcomes = [output.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(0, process.exitcode)
            self.assertEqual(1, outcomes.count("ok"))
            self.assertEqual(1, sum("reserves a worktree" in item for item in outcomes))

    def test_cross_worktree_common_store_and_idempotent_create(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as other:
            linked = Path(other) / "linked"
            _support.git(repo.path, "worktree", "add", "--detach", str(linked), "HEAD")
            first = FollowupStore.for_workspace(repo.path)
            second = FollowupStore.for_workspace(linked)
            self.assertEqual(first.root, second.root)
            self.assertNotEqual(first.context.git_dir, second.context.git_dir)

            contract, state = initial()
            self.assertEqual(0, first.create(FOLLOWUP_ID, contract, state)["state"]["revision"])
            updated = second.mutate(
                FOLLOWUP_ID,
                owner_id=OWNER_ID,
                expected_revision=0,
                request_id="request-1",
                payload={"action": "advance"},
                update=increment,
            )
            self.assertEqual(1, updated["state"]["revision"])
            self.assertEqual(updated, first.create(FOLLOWUP_ID, contract, state))
            self.assertEqual(updated, first.read(FOLLOWUP_ID))
            self.assertEqual(state["revision"], 0)
            with self.assertRaisesRegex(StateError, "different creation input"):
                first.create(FOLLOWUP_ID, contract, {**state, "phase": "different"})
            with self.assertRaisesRegex(StateError, "different creation input"):
                first.create(FOLLOWUP_ID, {**contract, "goal": "different"}, state)

    def test_receipt_idempotence_does_not_store_payload(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            contract, state = initial()
            store.create(FOLLOWUP_ID, contract, state)
            calls = 0

            def update(_contract: dict[str, object], current: dict[str, object]) -> None:
                nonlocal calls
                calls += 1
                increment(_contract, current)

            payload = {"reason": "sensitive-marker", "value": [1, 2]}
            first = store.mutate(
                FOLLOWUP_ID,
                owner_id=OWNER_ID,
                expected_revision=0,
                request_id="same-request",
                payload=payload,
                update=update,
            )
            retry = store.mutate(
                FOLLOWUP_ID,
                owner_id=OWNER_ID,
                expected_revision=0,
                request_id="same-request",
                payload={"value": [1, 2], "reason": "sensitive-marker"},
                update=update,
            )
            self.assertEqual(first, retry)
            self.assertEqual(1, calls)
            self.assertEqual(1, retry["state"]["revision"])
            journal = store.root / FOLLOWUP_ID / "journal.json"
            self.assertNotIn("sensitive-marker", journal.read_text(encoding="utf-8"))
            persisted = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual({"contract", "state"}, set(retry))
            self.assertEqual(64, len(persisted["receipts"]["same-request"]["payload_sha256"]))

            with self.assertRaisesRegex(StateError, "different payload"):
                store.mutate(
                    FOLLOWUP_ID,
                    owner_id=OWNER_ID,
                    expected_revision=0,
                    request_id="same-request",
                    payload={"reason": "changed"},
                    update=update,
                )
            with self.assertRaisesRegex(StateError, "owner_id mismatch"):
                store.mutate(
                    FOLLOWUP_ID,
                    owner_id="other-owner",
                    expected_revision=0,
                    request_id="same-request",
                    payload=payload,
                    update=update,
                )
            with self.assertRaisesRegex(StateError, "revision mismatch"):
                store.mutate(
                    FOLLOWUP_ID,
                    owner_id=OWNER_ID,
                    expected_revision=0,
                    request_id="new-request",
                    payload={},
                    update=update,
                )

    def test_multiprocess_cas_and_duplicate_request(self) -> None:
        context = multiprocessing.get_context("fork")
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            store.create(FOLLOWUP_ID, *initial())
            gate = context.Event()
            output = context.Queue()
            processes = [
                context.Process(
                    target=process_mutate,
                    args=(str(repo.path), gate, f"request-{index}", {"index": index}, output),
                )
                for index in range(4)
            ]
            for process in processes:
                process.start()
            gate.set()
            results = [output.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(0, process.exitcode)
            self.assertEqual(1, sum(result[0] == "ok" for result in results))
            self.assertEqual(1, store.read(FOLLOWUP_ID)["state"]["count"])

        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            store.create(FOLLOWUP_ID, *initial())
            gate = context.Event()
            output = context.Queue()
            processes = [
                context.Process(
                    target=process_mutate,
                    args=(str(repo.path), gate, "one-request", {"same": True}, output),
                )
                for _ in range(3)
            ]
            for process in processes:
                process.start()
            gate.set()
            results = [output.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(0, process.exitcode)
            self.assertEqual([("ok", 1)] * 3, results)
            self.assertEqual(1, store.read(FOLLOWUP_ID)["state"]["count"])

    def test_permissions_and_failed_replacement_recovery(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            store.create(FOLLOWUP_ID, *initial())
            for directory in (store.root.parent, store.root, store.root / FOLLOWUP_ID):
                self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
            for file in (store.root / ".lock", store.root / FOLLOWUP_ID / "journal.json"):
                self.assertEqual(0o600, stat.S_IMODE(file.stat().st_mode))

            with mock.patch("agent_harness.followup_store.os.replace", side_effect=OSError("before replace")):
                with self.assertRaisesRegex(OSError, "before replace"):
                    store.mutate(
                        FOLLOWUP_ID, owner_id=OWNER_ID, expected_revision=0,
                        request_id="retry", payload={"x": 1}, update=increment,
                    )
            self.assertEqual(0, store.read(FOLLOWUP_ID)["state"]["revision"])
            self.assertEqual([], list((store.root / FOLLOWUP_ID).glob(".journal.*")))

            actual_fsync = os.fsync

            def fail_after_replace(descriptor: int) -> None:
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise OSError("after replace")
                actual_fsync(descriptor)

            with mock.patch("agent_harness.followup_store.os.fsync", side_effect=fail_after_replace):
                with self.assertRaisesRegex(OSError, "after replace"):
                    store.mutate(
                        FOLLOWUP_ID, owner_id=OWNER_ID, expected_revision=0,
                        request_id="retry", payload={"x": 1}, update=increment,
                    )
            self.assertEqual(1, store.read(FOLLOWUP_ID)["state"]["revision"])
            again = store.mutate(
                FOLLOWUP_ID, owner_id=OWNER_ID, expected_revision=0,
                request_id="retry", payload={"x": 1}, update=increment,
            )
            self.assertEqual(1, again["state"]["count"])

    def test_failed_create_is_recoverable(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            contract, state = initial()
            with mock.patch("agent_harness.followup_store.os.replace", side_effect=OSError("create failed")):
                with self.assertRaisesRegex(OSError, "create failed"):
                    store.create(FOLLOWUP_ID, contract, state)
            self.assertFalse((store.root / FOLLOWUP_ID / "journal.json").exists())
            self.assertEqual(0, store.create(FOLLOWUP_ID, contract, state)["state"]["revision"])

    def test_corruption_traversal_and_symlinks_are_rejected(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as outside:
            store = FollowupStore.for_workspace(repo.path)
            contract, state = initial()
            for bad in ("", ".", "..", "../escape", "a/b", "a" * 129, "a\n", "é"):
                with self.subTest(id=bad), self.assertRaises(InputError):
                    store.create(bad, {**contract, "followup_id": bad}, state)
            with self.assertRaises(InputError):
                store.create(FOLLOWUP_ID, {**contract, "owner_id": "x" * 129}, state)
            with self.assertRaises(InputError):
                store.mutate(
                    FOLLOWUP_ID, owner_id=OWNER_ID, expected_revision=True,
                    request_id="request", payload={}, update=increment,
                )
            with self.assertRaises(InputError):
                store.mutate(
                    FOLLOWUP_ID, owner_id=OWNER_ID, expected_revision=0,
                    request_id="r" * 129, payload={}, update=increment,
                )

            store.create(FOLLOWUP_ID, contract, state)
            journal = store.root / FOLLOWUP_ID / "journal.json"
            original = journal.read_text(encoding="utf-8")
            for changed in (
                "not JSON",
                json.dumps({"contract": contract, "state": state}),
                original.replace('"revision": 0', '"revision": true'),
                original.replace(FOLLOWUP_ID, "wrong-id"),
                original.replace('"revision": 0', '"revision": NaN'),
                original.replace('"revision": 0', '"revision": 0, "revision": 0'),
            ):
                journal.write_text(changed, encoding="utf-8")
                with self.assertRaises(StateError):
                    store.read(FOLLOWUP_ID)
            journal.write_text(original, encoding="utf-8")

            journal.unlink()
            journal.symlink_to(Path(outside) / "target")
            with self.assertRaises(StateError):
                store.read(FOLLOWUP_ID)
            journal.unlink()
            (store.root / FOLLOWUP_ID).rmdir()
            (store.root / FOLLOWUP_ID).symlink_to(outside)
            with self.assertRaises(StateError):
                store.create(FOLLOWUP_ID, contract, state)
            (store.root / FOLLOWUP_ID).unlink()
            (store.root / ".lock").unlink()
            (store.root / ".lock").symlink_to(Path(outside) / "lock")
            with self.assertRaises(StateError):
                store.read(FOLLOWUP_ID)

        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as outside:
            context = resolve_repo(repo.path)
            (context.git_common_dir / "codex-agent-harness").symlink_to(outside)
            with self.assertRaises(StateError):
                FollowupStore(context).create(FOLLOWUP_ID, *initial())

    def test_update_cannot_change_contract_or_revision(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            store.create(FOLLOWUP_ID, *initial())

            def change_contract(contract: dict[str, object], _state: dict[str, object]) -> None:
                contract["goal"] = "changed"

            def change_revision(_contract: dict[str, object], state: dict[str, object]) -> None:
                state["revision"] = 10

            for update in (change_contract, change_revision):
                with self.assertRaises(StateError):
                    store.mutate(
                        FOLLOWUP_ID, owner_id=OWNER_ID, expected_revision=0,
                        request_id="attempt", payload={}, update=update,
                    )
            self.assertEqual(0, store.read(FOLLOWUP_ID)["state"]["revision"])

    def test_contract_corruption_after_mutation_is_detected(self) -> None:
        with _support.TempRepo() as repo:
            store = FollowupStore.for_workspace(repo.path)
            contract, state = initial()
            contract["tasks"] = {"A": {"workspace": str(repo.path)}}
            state["tasks"] = {"A": {"status": "pending"}}
            state["queued_sources"] = []
            store.create(FOLLOWUP_ID, contract, state)
            store.mutate(
                FOLLOWUP_ID, owner_id=OWNER_ID, expected_revision=0,
                request_id="mutate", payload={}, update=increment,
            )
            journal = store.root / FOLLOWUP_ID / "journal.json"
            original = json.loads(journal.read_text(encoding="utf-8"))
            for field, value in (
                ("owner_id", "other-owner"),
                ("tasks", {"A": {"workspace": "/different"}}),
                ("summary", "Changed"),
            ):
                with self.subTest(field=field):
                    damaged = json.loads(json.dumps(original))
                    damaged["contract"][field] = value
                    journal.write_text(json.dumps(damaged), encoding="utf-8")
                    with self.assertRaises(StateError):
                        store.read(FOLLOWUP_ID)
            journal.write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(1, store.read(FOLLOWUP_ID)["state"]["revision"])
