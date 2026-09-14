from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock

import _support

from agent_harness.followup import FollowupService
from agent_harness.git_repo import diff_fingerprint, resolve_repo
from agent_harness.service import HarnessService
from agent_harness.store import RunStore
from agent_harness.util import InputError, StateError


FOLLOWUP_ID = "corrections-1"
OWNER_ID = "owner-1"


def linked(repo: Path, parent: Path, name: str, sha: str) -> Path:
    path = parent / name
    _support.git(repo, "worktree", "add", "--detach", str(path), sha)
    return path


def commit(workspace: Path, name: str, content: str) -> str:
    (workspace / name).write_text(content, encoding="utf-8")
    _support.git(workspace, "add", name)
    _support.git(workspace, "commit", "-m", f"add {name}")
    return resolve_repo(workspace).head_sha


def completed_run(workspace: Path, base_sha: str) -> str:
    """Persist a real RunStore baseline fixture against the committed range."""
    service = HarnessService({})
    run_id = service.create_run({
        "workspace": str(workspace),
        "goal": "Verify the pinned baseline",
        "done_when": ["The baseline range is complete"],
        "base_sha": base_sha,
    })["contract"]["run_id"]
    return run_id


def seal_completed_run(workspace: Path, base_sha: str, run_id: str) -> None:
    store = RunStore.for_workspace(workspace)
    state = store.read_state(run_id)
    state["phase"] = "complete"
    state["terminal"] = {"status": "complete"}
    state["diff_fingerprint"] = diff_fingerprint(
        resolve_repo(workspace), base_sha=base_sha
    )[0]
    store.save_state(run_id, state)


def task(
    name: str, workspace: Path, base: str, head: str, *,
    dependencies: list[str] | None = None,
    published: bool = False,
    active: bool = False,
    run_id: str | None = None,
) -> dict:
    result = {
        "id": name,
        "workspace": str(workspace),
        "base_sha": base,
        "head_sha": head,
        "dependencies": dependencies or [],
        "published": published,
        "active": active,
        "build_checks": [f"build-{name}"],
        "test_checks": [f"unit-{name}"],
        "check_bindings": {check: hashlib.sha256(f"{check}:synthetic local conditions".encode()).hexdigest()
                           for check in (f"build-{name}", f"unit-{name}")},
    }
    if run_id is not None:
        result["run_id"] = run_id
    return result


@contextmanager
def chain(*, two: bool = False, proof: bool = False,
          correction: bool = False) -> Iterator[dict]:
    with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as temporary:
        temp = Path(temporary)
        source_sha = resolve_repo(repo.path).head_sha
        old_a = linked(repo.path, temp, "old-a", source_sha)
        run_a = completed_run(old_a, source_sha) if proof else None
        first_a = commit(old_a, "a.txt", "first\n")
        second_a = commit(old_a, "a.txt", "first\ncorrection\n") if correction else first_a
        if run_a:
            seal_completed_run(old_a, source_sha, run_a)
        result = {
            "repo": repo.path, "temp": temp, "source_sha": source_sha,
            "old_a": old_a, "a_head": second_a,
            "a_commits": [first_a, second_a] if correction else [first_a],
            "run_a": run_a,
        }
        if two:
            old_b = linked(repo.path, temp, "old-b", second_a)
            run_b = completed_run(old_b, second_a) if proof else None
            b_head = commit(old_b, "b.txt", "second\n")
            if run_b:
                seal_completed_run(old_b, second_a, run_b)
            result.update({"old_b": old_b, "b_head": b_head, "run_b": run_b})
        yield result


def create_service(fixture: dict, *, two: bool = False, published: bool = False,
                   active: bool = False, summary: str = "") -> FollowupService:
    service = FollowupService(str(fixture["repo"]))
    tasks = [task(
        "A", fixture["old_a"], fixture["source_sha"], fixture["a_head"],
        active=active, run_id=fixture["run_a"],
    )]
    if two:
        tasks.append(task(
            "B", fixture["old_b"], fixture["a_head"], fixture["b_head"],
            dependencies=["A"], published=published, run_id=fixture["run_b"],
        ))
    if fixture.get("extra_task"):
        tasks.append(fixture["extra_task"])
    service.create({
        "followup_id": FOLLOWUP_ID,
        "owner_id": OWNER_ID,
        "summary": summary,
        "source": {
            "workspace": str(fixture["repo"]),
            "base_sha": fixture["source_sha"],
            "head_sha": resolve_repo(fixture["repo"]).head_sha,
        },
        "tasks": tasks,
    })
    return service


def record(service: FollowupService, action: str, data: dict,
           *, request_id: str, expected_revision: int | None = None) -> dict:
    if expected_revision is None:
        expected_revision = service.store.read(FOLLOWUP_ID)["state"]["revision"]
    return service.record({
        "followup_id": FOLLOWUP_ID,
        "owner_id": OWNER_ID,
        "expected_revision": expected_revision,
        "request_id": request_id,
        "action": action,
        "data": data,
    })


def check(service: FollowupService, name: str, task_id: str,
          request_id: str, *, exit_code: int = 0) -> dict:
    fingerprint = service.store.read(FOLLOWUP_ID)["state"]["tasks"][task_id]["candidate"]["diff_fingerprint"]
    return record(service, "check", {
        "task_id": task_id, "name": name,
        "diff_fingerprint": fingerprint, "exit_code": exit_code,
        "check_fingerprint": service.store.read(FOLLOWUP_ID)["state"]["tasks"][task_id]["check_bindings"][name],
        "duration_ms": 1,
    }, request_id=request_id)


class FollowupServiceTests(unittest.TestCase):
    def test_queued_source_is_reserved_and_revalidated_before_advance(self) -> None:
        with chain() as fixture:
            service = create_service(fixture)
            queued = linked(fixture["repo"], fixture["temp"], "queued", fixture["source_sha"])
            head = commit(queued, "parent.txt", "next parent\n")
            record(service, "queue_source", {"source": {"workspace": str(queued), "base_sha": fixture["source_sha"], "head_sha": head}}, request_id="queue")
            with self.assertRaisesRegex(StateError, "own worktree"):
                record(service, "begin", {"task_id": "A", "workspace": str(queued), "mode": "semantic"}, request_id="overwrite-source")
            commit(queued, "parent.txt", "unexpected source drift\n")
            before = service.store.read(FOLLOWUP_ID)["state"]
            with self.assertRaisesRegex(StateError, "queued source changed"):
                record(service, "advance", {}, request_id="stale-advance")
            self.assertEqual(before, service.store.read(FOLLOWUP_ID)["state"])

    def test_queue_cannot_pin_an_already_claimed_adaptation_worktree(self) -> None:
        with chain() as fixture:
            service = create_service(fixture)
            target = linked(fixture["repo"], fixture["temp"], "target", fixture["a_head"])
            record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "semantic"}, request_id="begin")
            with self.assertRaisesRegex(StateError, "separate from all task"):
                record(service, "queue_source", {"source": {"workspace": str(target), "base_sha": fixture["source_sha"], "head_sha": fixture["a_head"]}}, request_id="queue-target")

    def test_abandon_preserves_history_releases_reservations_and_is_terminal(self) -> None:
        with chain() as fixture:
            service = create_service(fixture)
            original_contract = service.store.read(FOLLOWUP_ID)["contract"]
            target = linked(fixture["repo"], fixture["temp"], "target", fixture["a_head"])
            record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "semantic"}, request_id="begin")
            with self.assertRaisesRegex(StateError, "safe checkpoints"):
                record(service, "abandon", {"summary": "cancel the correction"}, request_id="unsafe-cancel")
            record(service, "pause", {"task_id": "A", "status": "interrupted"}, request_id="pause")
            cancelled = record(service, "abandon", {"summary": "cancel the correction"}, request_id="cancel")
            repeated = record(service, "abandon", {"summary": "cancel the correction"}, request_id="cancel")
            self.assertEqual(cancelled, repeated)
            self.assertEqual(original_contract, cancelled["contract"])
            self.assertEqual("cancelled", cancelled["state"]["terminal"]["status"])
            self.assertFalse(service.get(FOLLOWUP_ID)["readiness"]["A"]["ready"])
            self.assertEqual([], service.get(FOLLOWUP_ID)["transfer_ready_tasks"])
            with self.assertRaisesRegex(StateError, "cancelled"):
                record(service, "progress", {"bucket": "writing", "duration_ms": 1}, request_id="after-cancel")
            replacement = service.create({"followup_id": "replacement", "owner_id": OWNER_ID,
                "source": {"workspace": str(fixture["repo"]), "base_sha": fixture["source_sha"], "head_sha": fixture["source_sha"]},
                "tasks": [task("A", fixture["old_a"], fixture["source_sha"], fixture["a_head"])]})
            self.assertEqual("replacement", replacement["contract"]["followup_id"])
            self.assertEqual(cancelled, create_service(fixture).store.read(FOLLOWUP_ID))

    def test_two_tasks_cannot_claim_one_adaptation_worktree_before_candidate(self) -> None:
        with chain() as fixture:
            other = linked(fixture["repo"], fixture["temp"], "other", fixture["source_sha"])
            other_head = commit(other, "other.txt", "independent task\n")
            service = FollowupService(str(fixture["repo"]))
            service.create({"followup_id": FOLLOWUP_ID, "owner_id": OWNER_ID,
                "source": {"workspace": str(fixture["repo"]), "base_sha": fixture["source_sha"], "head_sha": fixture["source_sha"]},
                "tasks": [task("A", fixture["old_a"], fixture["source_sha"], fixture["a_head"]),
                          task("B", other, fixture["source_sha"], other_head)]})
            target = linked(fixture["repo"], fixture["temp"], "target", fixture["a_head"])
            record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "semantic"}, request_id="begin-a")
            for status in ("adapting", "interrupted"):
                if status == "interrupted":
                    record(service, "pause", {"task_id": "A", "status": status}, request_id="pause-a")
                with self.assertRaisesRegex(StateError, "own worktree"):
                    record(service, "begin", {"task_id": "B", "workspace": str(target), "mode": "semantic"}, request_id=f"collision-{status}")

    def test_reopen_after_leaf_failure_rebuilds_only_the_affected_chain(self) -> None:
        with chain(two=True, proof=True) as fixture:
            independent = linked(fixture["repo"], fixture["temp"], "independent", fixture["source_sha"])
            independent_head = commit(independent, "independent.txt", "active unrelated task\n")
            fixture["extra_task"] = task("D", independent, fixture["source_sha"], independent_head, active=True)
            service = create_service(fixture, two=True)
            independent_state = service.store.read(FOLLOWUP_ID)["state"]["tasks"]["D"]
            for name, head in (("A", fixture["a_head"]), ("B", fixture["b_head"])):
                target = linked(fixture["repo"], fixture["temp"], f"first-{name}", head)
                record(service, "begin", {"task_id": name, "workspace": str(target), "mode": "mechanical"}, request_id=f"begin-{name}")
                record(service, "candidate", {"task_id": name}, request_id=f"candidate-{name}")
                check(service, f"build-{name}", name, f"build-{name}")
                if name == "A":
                    record(service, "finish", {"task_id": name}, request_id="finish-A")
            check(service, "unit-A", "B", "failed-chain-test", exit_code=1)
            with self.assertRaisesRegex(StateError, "safe checkpoints"):
                record(service, "reopen", {"task_id": "A", "summary": "fix inherited compatibility"}, request_id="unsafe-reopen")
            record(service, "pause", {"task_id": "B", "status": "blocked"}, request_id="pause-B")
            reopened = record(service, "reopen", {"task_id": "A", "summary": "fix inherited compatibility"}, request_id="reopen")
            self.assertEqual(1, reopened["state"]["epoch"])
            self.assertEqual(fixture["source_sha"], reopened["state"]["source"]["head_sha"])
            self.assertEqual("pending", reopened["state"]["tasks"]["A"]["status"])
            self.assertEqual({}, reopened["state"]["tasks"]["B"]["checks"])
            self.assertEqual("mechanical_checked", reopened["state"]["history"][-1]["tasks"]["A"]["status"])
            parent = None
            for name, head in (("A", fixture["a_head"]), ("B", fixture["b_head"])):
                target = linked(fixture["repo"], fixture["temp"], f"second-{name}", head)
                record(service, "begin", {"task_id": name, "workspace": str(target), "mode": "mechanical"}, request_id=f"retry-{name}")
                if name == "A":
                    parent = commit(target, "compatibility.txt", "mechanical compatibility repair\n")
                else:
                    _support.git(target, "merge", "--no-edit", parent)
                record(service, "candidate", {"task_id": name}, request_id=f"new-candidate-{name}")
                check(service, f"build-{name}", name, f"new-build-{name}")
                if name == "B":
                    for check_name in ("unit-A", "unit-B"):
                        check(service, check_name, "B", f"new-{check_name}")
                record(service, "finish", {"task_id": name}, request_id=f"new-finish-{name}")
            readiness = service.get(FOLLOWUP_ID)["readiness"]
            self.assertTrue(all(readiness[key]["ready"] for key in ("A", "B")))
            again = record(service, "reopen", {"task_id": "A", "summary": "another compatibility repair"}, request_id="again")
            self.assertTrue(all(again["state"]["tasks"][key]["status"] == "pending" for key in ("A", "B")))
            self.assertTrue(all(again["state"]["tasks"][key]["attempt"] == 2 for key in ("A", "B")))
            self.assertEqual(independent_state, again["state"]["tasks"]["D"])

    def test_fanout_keeps_independent_sibling_moving_while_writer_is_active(self) -> None:
        with chain(two=True, proof=True) as fixture:
            old_c = linked(fixture["repo"], fixture["temp"], "old-c", fixture["a_head"])
            run_c = completed_run(old_c, fixture["a_head"])
            c_head = commit(old_c, "c.txt", "independent sibling\n")
            seal_completed_run(old_c, fixture["a_head"], run_c)
            service = FollowupService(str(fixture["repo"]))
            service.create({"followup_id": FOLLOWUP_ID, "owner_id": OWNER_ID,
                "source": {"workspace": str(fixture["repo"]), "base_sha": fixture["source_sha"], "head_sha": fixture["source_sha"]},
                "tasks": [task("A", fixture["old_a"], fixture["source_sha"], fixture["a_head"], run_id=fixture["run_a"]),
                          task("B", fixture["old_b"], fixture["a_head"], fixture["b_head"], dependencies=["A"], active=True),
                          task("C", old_c, fixture["a_head"], c_head, dependencies=["A"], run_id=run_c)]})
            self.assertEqual(["A"], [item["task_id"] for item in service.get(FOLLOWUP_ID)["transfer_ready_tasks"]])
            for task_id, head in (("A", fixture["a_head"]), ("C", c_head)):
                target = linked(fixture["repo"], fixture["temp"], f"target-{task_id}", head)
                record(service, "begin", {"task_id": task_id, "workspace": str(target), "mode": "mechanical"}, request_id=f"begin-{task_id}")
                record(service, "candidate", {"task_id": task_id}, request_id=f"candidate-{task_id}")
                check(service, f"build-{task_id}", task_id, f"build-{task_id}")
                if task_id == "C":
                    check(service, "unit-A", "C", "inherited-test")
                    check(service, "unit-C", "C", "own-test")
                record(service, "finish", {"task_id": task_id}, request_id=f"finish-{task_id}")
                if task_id == "A":
                    self.assertEqual(["C"], [item["task_id"] for item in service.get(FOLLOWUP_ID)["transfer_ready_tasks"]])
            result = service.get(FOLLOWUP_ID)
            self.assertEqual("awaiting_checkpoint", result["state"]["tasks"]["B"]["status"])
            self.assertFalse(result["readiness"]["A"]["ready"])
            self.assertTrue(result["readiness"]["C"]["ready"])

    def test_resume_preserves_late_commits_and_cannot_downgrade_semantic_mode(self) -> None:
        with chain(proof=True) as fixture:
            service = create_service(fixture)
            target = linked(fixture["repo"], fixture["temp"], "target", fixture["a_head"])
            record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "semantic"}, request_id="begin")
            late = commit(target, "late.txt", "semantic correction\n")
            record(service, "pause", {"task_id": "A", "status": "interrupted"}, request_id="pause")
            with self.assertRaisesRegex(StateError, "semantic review"):
                record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "mechanical"}, request_id="downgrade")
            stale = linked(fixture["repo"], fixture["temp"], "stale", fixture["a_head"])
            with self.assertRaisesRegex(StateError, "full interrupted checkpoint"):
                record(service, "begin", {"task_id": "A", "workspace": str(stale), "mode": "semantic"}, request_id="lost-late")
            resumed = record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "semantic"}, request_id="resume")
            self.assertIn(late, resumed["state"]["tasks"]["A"]["preserved_commits"])
            record(service, "candidate", {"task_id": "A"}, request_id="candidate")

    def test_dirty_pause_requires_exact_isolated_seed_before_resume(self) -> None:
        with chain(proof=True) as fixture:
            service = create_service(fixture)
            target = linked(fixture["repo"], fixture["temp"], "target", fixture["a_head"])
            record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "semantic"}, request_id="begin")
            (target / "late.txt").write_text("uncommitted correction\n", encoding="utf-8")
            record(service, "pause", {"task_id": "A", "status": "interrupted"}, request_id="pause")
            isolated = linked(fixture["repo"], fixture["temp"], "isolated", fixture["a_head"])
            with self.assertRaisesRegex(StateError, "exact snapshot"):
                record(service, "begin", {"task_id": "A", "workspace": str(isolated), "mode": "semantic"}, request_id="lost-dirty")
            seed = commit(isolated, "late.txt", "uncommitted correction\n")
            resumed = record(service, "begin", {"task_id": "A", "workspace": str(isolated), "mode": "semantic"}, request_id="resume")
            self.assertIn(seed, resumed["state"]["tasks"]["A"]["preserved_commits"])
            self.assertEqual("?? late.txt", _support.git(target, "status", "--porcelain"))
            record(service, "candidate", {"task_id": "A"}, request_id="candidate")

    def test_service_create_recovers_directory_left_before_first_replace(self) -> None:
        with chain() as fixture:
            with mock.patch("agent_harness.followup_store.FollowupStore._atomic_write", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    create_service(fixture)
            service = create_service(fixture)
            self.assertEqual(0, service.get(FOLLOWUP_ID)["state"]["revision"])

    def test_check_names_cannot_make_a_leaf_build_count_as_an_ancestor_test(self) -> None:
        with chain(two=True) as fixture:
            service = FollowupService(str(fixture["repo"]))
            first = task("A", fixture["old_a"], fixture["source_sha"], fixture["a_head"])
            second = task("B", fixture["old_b"], fixture["a_head"], fixture["b_head"], dependencies=["A"])
            second["build_checks"] = first["test_checks"]
            second["check_bindings"] = {**first["check_bindings"], "unit-B": second["check_bindings"]["unit-B"]}
            second["check_bindings"].pop("build-A")
            with self.assertRaises(InputError):
                service.create({"followup_id": FOLLOWUP_ID, "owner_id": OWNER_ID,
                                "source": {"workspace": str(fixture["repo"]), "base_sha": fixture["source_sha"],
                                           "head_sha": fixture["source_sha"]}, "tasks": [first, second]})

    def test_check_must_match_frozen_command_and_conditions(self) -> None:
        with chain(proof=True) as fixture:
            service = create_service(fixture)
            target = linked(fixture["repo"], fixture["temp"], "target", fixture["a_head"])
            record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "mechanical"}, request_id="begin")
            result = record(service, "candidate", {"task_id": "A"}, request_id="candidate")
            fingerprint = result["state"]["tasks"]["A"]["candidate"]["diff_fingerprint"]
            with self.assertRaises(StateError):
                record(service, "check", {"task_id": "A", "name": "build-A", "diff_fingerprint": fingerprint,
                       "check_fingerprint": "f" * 64, "exit_code": 0, "duration_ms": 1}, request_id="wrong-context")
            self.assertEqual({}, service.get(FOLLOWUP_ID)["state"]["tasks"]["A"]["checks"])
            check(service, "build-A", "A", "right-context")

    def test_two_mechanical_descendants_require_build_and_leaf_union_tests(self) -> None:
        with chain(two=True, proof=True) as fixture:
            service = create_service(fixture, two=True, published=True)
            a_target = linked(fixture["repo"], fixture["temp"], "target-a", fixture["a_head"])
            b_target = linked(fixture["repo"], fixture["temp"], "target-b", fixture["b_head"])
            original_heads = {
                str(path): resolve_repo(path).head_sha
                for path in (fixture["repo"], fixture["old_a"], fixture["old_b"], a_target, b_target)
            }
            actual_run = subprocess.run
            invoked: list[str] = []

            def git_only(argv: list[str], *args: object, **kwargs: object):
                invoked.append(argv[0])
                self.assertEqual("git", argv[0], "service must not launch a model or Git writer")
                return actual_run(argv, *args, **kwargs)

            with mock.patch("agent_harness.git_repo.subprocess.run", side_effect=git_only):
                record(service, "begin", {"task_id": "A", "workspace": str(a_target), "mode": "mechanical"}, request_id="begin-a")
                record(service, "candidate", {"task_id": "A"}, request_id="candidate-a")
                check(service, "unit-A", "A", "unit-a")
                with self.assertRaisesRegex(StateError, "checks"):
                    record(service, "finish", {"task_id": "A"}, request_id="false-green-a")
                check(service, "build-A", "A", "build-a")
                record(service, "finish", {"task_id": "A"}, request_id="finish-a")
                record(service, "begin", {"task_id": "B", "workspace": str(b_target), "mode": "mechanical"}, request_id="begin-b")
                record(service, "candidate", {"task_id": "B"}, request_id="candidate-b")
                check(service, "build-B", "B", "build-b")
                check(service, "unit-B", "B", "unit-b")
                with self.assertRaisesRegex(StateError, "checks"):
                    record(service, "finish", {"task_id": "B"}, request_id="missing-ancestor-test")
                check(service, "unit-A", "B", "ancestor-unit-b")
                record(service, "finish", {"task_id": "B"}, request_id="finish-b")
                result = service.get(FOLLOWUP_ID)
            self.assertTrue(invoked and set(invoked) == {"git"})
            self.assertEqual("mechanical_checked", result["state"]["tasks"]["A"]["status"])
            self.assertEqual("mechanical_checked", result["state"]["tasks"]["B"]["status"])
            self.assertTrue(result["readiness"]["A"]["ready"])
            self.assertTrue(result["readiness"]["B"]["ready"])
            self.assertFalse(result["external_actions_authorized"])
            for workspace, sha in original_heads.items():
                self.assertEqual(sha, resolve_repo(workspace).head_sha)
                self.assertEqual("", _support.git(Path(workspace), "status", "--porcelain"))

    def test_published_candidate_merge_preserves_old_history(self) -> None:
        with chain() as fixture:
            service = FollowupService(str(fixture["repo"]))
            service.create({
                "followup_id": FOLLOWUP_ID, "owner_id": OWNER_ID,
                "source": {"workspace": str(fixture["repo"]),
                           "base_sha": fixture["source_sha"], "head_sha": fixture["source_sha"]},
                "tasks": [task("A", fixture["old_a"], fixture["source_sha"],
                               fixture["a_head"], published=True)],
            })
            target = linked(fixture["repo"], fixture["temp"], "target", fixture["a_head"])
            extra = linked(fixture["repo"], fixture["temp"], "extra", fixture["source_sha"])
            extra_sha = commit(extra, "extra.txt", "other branch\n")
            _support.git(target, "merge", "--no-ff", "-m", "merge source", extra_sha)
            record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "semantic"}, request_id="begin")
            result = record(service, "candidate", {"task_id": "A"}, request_id="candidate")
            self.assertEqual(resolve_repo(target).head_sha, result["state"]["tasks"]["A"]["candidate"]["head_sha"])
            self.assertEqual("", _support.git(target, "status", "--porcelain"))

    def test_unpublished_replay_maps_every_own_commit_including_correction(self) -> None:
        with chain(correction=True) as fixture:
            source_new = commit(fixture["repo"], "source.txt", "new source\n")
            service = create_service(fixture)
            target = linked(fixture["repo"], fixture["temp"], "target", source_new)
            mapped = []
            for old_sha in fixture["a_commits"]:
                _support.git(target, "cherry-pick", old_sha)
                mapped.append({"old_sha": old_sha, "new_sha": resolve_repo(target).head_sha})
            record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "semantic"}, request_id="begin")
            with self.assertRaisesRegex(StateError, "full task range"):
                record(service, "candidate", {"task_id": "A", "replayed_commits": mapped[:1]}, request_id="missing-correction")
            result = record(service, "candidate", {"task_id": "A", "replayed_commits": mapped}, request_id="full-replay")
            self.assertEqual(mapped, result["state"]["tasks"]["A"]["candidate"]["replayed_commits"])

    def test_missing_own_commit_is_rejected(self) -> None:
        with chain() as fixture:
            service = create_service(fixture)
            target = linked(fixture["repo"], fixture["temp"], "target", fixture["source_sha"])
            commit(target, "unrelated.txt", "not the original task\n")
            record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "semantic"}, request_id="begin")
            with self.assertRaisesRegex(StateError, "full task range"):
                record(service, "candidate", {"task_id": "A"}, request_id="candidate")

    def test_unknown_fields_and_secrets_are_not_persisted(self) -> None:
        with chain() as fixture:
            service = create_service(fixture, summary="token=visible-secret")
            with self.assertRaises(InputError):
                service.create({"followup_id": FOLLOWUP_ID, "unsupported": True})
            with self.assertRaises(InputError):
                record(service, "progress", {"bucket": "writing", "duration_ms": 1,
                                             "unexpected": "secret"}, request_id="bad")
            result = record(service, "progress", {
                "bucket": "writing", "duration_ms": 10,
                "summary": "password=another-secret",
            }, request_id="progress")
            self.assertNotIn("visible-secret", str(result))
            self.assertNotIn("another-secret", str(result))
            journal = service.store.root / FOLLOWUP_ID / "journal.json"
            self.assertNotIn("visible-secret", journal.read_text(encoding="utf-8"))
            self.assertNotIn("another-secret", journal.read_text(encoding="utf-8"))

    def test_progress_checkpoint_uses_unreported_active_time(self) -> None:
        with chain() as fixture:
            service = create_service(fixture)
            record(service, "progress", {
                "bucket": "writing", "duration_ms": 900_000,
            }, request_id="work-1")
            self.assertTrue(service.get(FOLLOWUP_ID)["progress_checkpoint_due"])
            record(service, "progress", {
                "bucket": "external_wait", "duration_ms": 3_600_000,
            }, request_id="external-wait")
            self.assertTrue(service.get(FOLLOWUP_ID)["progress_checkpoint_due"])
            reported = record(service, "progress", {
                "bucket": "writing", "duration_ms": 0,
                "summary": "token=private-status",
            }, request_id="report")
            self.assertEqual(900_000, reported["state"]["reported_active_ms"])
            self.assertNotIn("private-status", str(reported))
            self.assertFalse(service.get(FOLLOWUP_ID)["progress_checkpoint_due"])
            record(service, "progress", {
                "bucket": "checks", "duration_ms": 899_999,
            }, request_id="work-2")
            self.assertFalse(service.get(FOLLOWUP_ID)["progress_checkpoint_due"])
            record(service, "progress", {
                "bucket": "review", "duration_ms": 1,
            }, request_id="work-3")
            self.assertTrue(service.get(FOLLOWUP_ID)["progress_checkpoint_due"])

    def test_active_checkpoint_and_queued_source_safe_boundary(self) -> None:
        with chain(proof=True) as fixture:
            service = create_service(fixture, active=True)
            target = linked(fixture["repo"], fixture["temp"], "target", fixture["a_head"])
            with self.assertRaisesRegex(StateError, "safe checkpoint"):
                record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "mechanical"}, request_id="early-begin")
            record(service, "checkpoint", {"task_id": "A", "released": True}, request_id="release")
            record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "mechanical"}, request_id="begin")
            source_new = commit(fixture["repo"], "source.txt", "new source\n")
            queued = record(service, "queue_source", {"source": {
                "workspace": str(fixture["repo"]), "base_sha": fixture["source_sha"],
                "head_sha": source_new,
            }}, request_id="queue")
            self.assertEqual(fixture["source_sha"], queued["state"]["tasks"]["A"]["parent_sha"])
            self.assertEqual(fixture["source_sha"], queued["state"]["source"]["head_sha"])
            self.assertFalse(service.get(FOLLOWUP_ID)["readiness"]["A"]["ready"])
            with self.assertRaisesRegex(StateError, "advance requires"):
                record(service, "advance", {}, request_id="unsafe-advance")
            record(service, "pause", {"task_id": "A", "status": "interrupted"}, request_id="pause")
            advanced = record(service, "advance", {}, request_id="advance")
            self.assertEqual(source_new, advanced["state"]["source"]["head_sha"])
            self.assertEqual(2, advanced["state"]["epoch"])
            self.assertEqual(1, len(advanced["state"]["history"]))
            preserved = advanced["state"]["tasks"]["A"]["checkpoint_baseline"]
            self.assertEqual(str(target.resolve()), preserved["workspace"])
            self.assertEqual(fixture["a_head"], preserved["head_sha"])
            self.assertIsNone(preserved["verified_baseline"])
            next_target = linked(fixture["repo"], fixture["temp"], "next-target", source_new)
            _support.git(next_target, "cherry-pick", fixture["a_head"])
            begun = record(service, "begin", {
                "task_id": "A", "workspace": str(next_target), "mode": "semantic",
            }, request_id="next-begin")
            self.assertEqual(source_new, begun["state"]["tasks"]["A"]["parent_sha"])
            self.assertNotEqual(str(target.resolve()), begun["state"]["tasks"]["A"]["workspace"])

    def test_source_and_candidate_drift_invalidate_readiness(self) -> None:
        for drift in ("source", "candidate"):
            with self.subTest(drift=drift), chain(proof=True) as fixture:
                service = create_service(fixture)
                target = linked(fixture["repo"], fixture["temp"], "target", fixture["a_head"])
                record(service, "begin", {"task_id": "A", "workspace": str(target), "mode": "mechanical"}, request_id="begin")
                record(service, "candidate", {"task_id": "A"}, request_id="candidate")
                check(service, "build-A", "A", "build")
                check(service, "unit-A", "A", "unit")
                record(service, "finish", {"task_id": "A"}, request_id="finish")
                self.assertTrue(service.get(FOLLOWUP_ID)["readiness"]["A"]["ready"])
                changed = fixture["repo"] if drift == "source" else target
                (changed / "untracked.txt").write_text("drift\n", encoding="utf-8")
                self.assertFalse(service.get(FOLLOWUP_ID)["readiness"]["A"]["ready"])

    def test_duplicate_request_after_git_changed_does_not_reapply(self) -> None:
        with chain() as fixture:
            service = create_service(fixture)
            source_new = commit(fixture["repo"], "source.txt", "first\n")
            data = {"source": {"workspace": str(fixture["repo"]),
                               "base_sha": fixture["source_sha"], "head_sha": source_new}}
            first = record(service, "queue_source", data, request_id="queue", expected_revision=0)
            commit(fixture["repo"], "another.txt", "second\n")
            repeated = record(service, "queue_source", data, request_id="queue", expected_revision=0)
            self.assertEqual(first, repeated)
            self.assertEqual(1, repeated["state"]["revision"])
            self.assertEqual([source_new], [item["head_sha"] for item in repeated["state"]["queued_sources"]])

    def test_active_writer_checkpoint_freezes_new_full_range_without_editing_contract(self) -> None:
        with chain(proof=True) as fixture:
            service = create_service(fixture, active=True)
            original = service.store.read(FOLLOWUP_ID)["contract"]
            correction_sha = commit(fixture["old_a"], "a.txt", "first\nlate correction\n")
            released = record(service, "checkpoint", {
                "task_id": "A", "released": True,
            }, request_id="checkpoint")
            effective = released["state"]["tasks"]["A"]["checkpoint_baseline"]
            self.assertEqual(correction_sha, effective["head_sha"])
            self.assertEqual(fixture["a_commits"] + [correction_sha], effective["own_commits"])
            self.assertIsNone(effective["verified_baseline"])
            self.assertEqual(original, released["contract"])
            self.assertEqual(fixture["a_head"], released["contract"]["tasks"]["A"]["head_sha"])
            target = linked(fixture["repo"], fixture["temp"], "target", correction_sha)
            with self.assertRaisesRegex(StateError, "verified baseline"):
                record(service, "begin", {
                    "task_id": "A", "workspace": str(target), "mode": "mechanical",
                }, request_id="mechanical-with-stale-proof")
            record(service, "begin", {
                "task_id": "A", "workspace": str(target), "mode": "semantic",
            }, request_id="semantic")
            candidate = record(service, "candidate", {"task_id": "A"}, request_id="candidate")
            self.assertEqual(correction_sha, candidate["state"]["tasks"]["A"]["candidate"]["head_sha"])

    def test_create_retry_after_source_and_candidate_drift_is_read_only(self) -> None:
        with chain() as fixture:
            service = FollowupService(str(fixture["repo"]))
            arguments = {
                "followup_id": FOLLOWUP_ID, "owner_id": OWNER_ID,
                "summary": "same creation request",
                "source": {
                    "workspace": str(fixture["repo"]),
                    "base_sha": fixture["source_sha"],
                    "head_sha": fixture["source_sha"],
                },
                "tasks": [task("A", fixture["old_a"], fixture["source_sha"], fixture["a_head"])],
            }
            service.create(arguments)
            target = linked(fixture["repo"], fixture["temp"], "target", fixture["a_head"])
            record(service, "begin", {
                "task_id": "A", "workspace": str(target), "mode": "semantic",
            }, request_id="begin")
            recorded = record(service, "candidate", {"task_id": "A"}, request_id="candidate")
            commit(fixture["repo"], "new-source.txt", "source drift\n")
            (target / "dirty.txt").write_text("candidate drift\n", encoding="utf-8")
            with mock.patch("agent_harness.followup.source_snapshot", side_effect=AssertionError("recalculated")):
                repeated = service.create(arguments)
            self.assertEqual(recorded, repeated)
            with self.assertRaisesRegex(StateError, "different request"):
                service.create({**arguments, "summary": "changed"})

    def test_new_parent_replays_full_previous_candidate_and_rechecks(self) -> None:
        with chain(proof=True) as fixture:
            service = create_service(fixture)
            first_target = linked(fixture["repo"], fixture["temp"], "first-target", fixture["a_head"])
            adapted_correction = commit(first_target, "a.txt", "first\nadapted correction\n")
            record(service, "begin", {
                "task_id": "A", "workspace": str(first_target), "mode": "mechanical",
            }, request_id="first-begin")
            record(service, "candidate", {"task_id": "A"}, request_id="first-candidate")
            check(service, "build-A", "A", "first-build")
            check(service, "unit-A", "A", "first-unit")
            completed = record(service, "finish", {"task_id": "A"}, request_id="first-finish")
            self.assertEqual("mechanical_checked", completed["state"]["tasks"]["A"]["status"])
            source_new = commit(fixture["repo"], "source.txt", "new source\n")
            record(service, "queue_source", {"source": {
                "workspace": str(fixture["repo"]),
                "base_sha": fixture["source_sha"], "head_sha": source_new,
            }}, request_id="queue")
            advanced = record(service, "advance", {}, request_id="advance")
            current = advanced["state"]["tasks"]["A"]
            new_baseline = current["checkpoint_baseline"]
            self.assertEqual(str(first_target.resolve()), new_baseline["workspace"])
            self.assertEqual(adapted_correction, new_baseline["head_sha"])
            self.assertEqual(fixture["a_commits"] + [adapted_correction], new_baseline["own_commits"])
            self.assertEqual({}, current["checks"])
            self.assertEqual(1, len(advanced["state"]["history"]))
            second_target = linked(fixture["repo"], fixture["temp"], "second-target", source_new)
            mapping = []
            for old_sha in new_baseline["own_commits"]:
                _support.git(second_target, "cherry-pick", old_sha)
                mapping.append({"old_sha": old_sha, "new_sha": resolve_repo(second_target).head_sha})
            record(service, "begin", {
                "task_id": "A", "workspace": str(second_target), "mode": "semantic",
            }, request_id="second-begin")
            with self.assertRaisesRegex(StateError, "full task range"):
                record(service, "candidate", {
                    "task_id": "A", "replayed_commits": mapping[:1],
                }, request_id="incomplete-second-replay")
            second = record(service, "candidate", {
                "task_id": "A", "replayed_commits": mapping,
            }, request_id="second-candidate")
            self.assertEqual(mapping, second["state"]["tasks"]["A"]["candidate"]["replayed_commits"])
            self.assertEqual({}, second["state"]["tasks"]["A"]["checks"])
            self.assertEqual(source_new, second["state"]["tasks"]["A"]["parent_sha"])
            self.assertNotEqual(str(first_target.resolve()), second["state"]["tasks"]["A"]["workspace"])
            with self.assertRaisesRegex(StateError, "checks"):
                record(service, "finish", {"task_id": "A"}, request_id="stale-checks")

    def test_dirty_active_checkpoint_requires_exact_seed_and_preserves_index(self) -> None:
        with chain() as fixture:
            service = create_service(fixture, active=True)
            old = fixture["old_a"]
            latefix = commit(old, "a.txt", "first\nlate fix\n")
            staged = old / "staged.txt"
            staged.write_text("staged version\n", encoding="utf-8")
            _support.git(old, "add", "staged.txt")
            staged.write_text("working version\n", encoding="utf-8")
            (old / "untracked.txt").write_text("untracked version\n", encoding="utf-8")
            (old / "a.txt").write_text("first\nlate fix\nworking edit\n", encoding="utf-8")
            before_status = _support.git(old, "status", "--porcelain")
            index_name = _support.git(old, "rev-parse", "--path-format=absolute", "--git-path", "index")
            index_path = Path(index_name)
            before_index = hashlib.sha256(index_path.read_bytes()).hexdigest()
            released = record(service, "checkpoint", {
                "task_id": "A", "released": True,
            }, request_id="dirty-checkpoint")
            baseline = released["state"]["tasks"]["A"]["checkpoint_baseline"]
            self.assertEqual(latefix, baseline["head_sha"])
            self.assertEqual(fixture["a_commits"] + [latefix], baseline["own_commits"])
            self.assertFalse(baseline["baseline_snapshot"]["clean"])
            self.assertTrue({"a.txt", "staged.txt", "untracked.txt"} <= set(baseline["dirty_manifest"]))
            self.assertEqual(before_index, hashlib.sha256(index_path.read_bytes()).hexdigest())
            self.assertEqual(before_status, _support.git(old, "status", "--porcelain"))

            target = linked(fixture["repo"], fixture["temp"], "seed-target", latefix)
            with self.assertRaisesRegex(StateError, "clean exact snapshot commit"):
                record(service, "begin", {
                    "task_id": "A", "workspace": str(target), "mode": "semantic",
                }, request_id="without-seed")
            for name in ("a.txt", "staged.txt", "untracked.txt"):
                (target / name).write_bytes((old / name).read_bytes())
            _support.git(target, "add", "-A")
            _support.git(target, "commit", "-m", "preserve dirty checkpoint")
            seed_sha = resolve_repo(target).head_sha
            begun = record(service, "begin", {
                "task_id": "A", "workspace": str(target), "mode": "semantic",
            }, request_id="with-seed")
            self.assertIn(seed_sha, begun["state"]["tasks"]["A"]["preserved_commits"])
            _support.git(target, "switch", "--detach", latefix)
            with self.assertRaisesRegex(StateError, "full task range"):
                record(service, "candidate", {"task_id": "A"}, request_id="dropped-seed")
            _support.git(target, "switch", "--detach", seed_sha)
            accepted = record(service, "candidate", {"task_id": "A"}, request_id="kept-seed")
            self.assertEqual(seed_sha, accepted["state"]["tasks"]["A"]["candidate"]["head_sha"])
            self.assertEqual(before_index, hashlib.sha256(index_path.read_bytes()).hexdigest())
            self.assertEqual(before_status, _support.git(old, "status", "--porcelain"))
