from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import _support

from agent_harness.contract import build_contract
from agent_harness.git_repo import diff_fingerprint, resolve_repo
from agent_harness.policy import plan_checks, validate_checks
from agent_harness.service import HarnessService
from agent_harness.store import RunStore
from agent_harness.util import InputError, StateError


class GitFingerprintTests(unittest.TestCase):
    def test_fingerprint_tracks_tracked_and_untracked_content(self) -> None:
        with _support.TempRepo() as repo:
            context = resolve_repo(repo.path)
            clean, paths = diff_fingerprint(context)
            self.assertEqual([], paths)

            (repo.path / "README.md").write_text("changed\n", encoding="utf-8")
            tracked, paths = diff_fingerprint(context)
            self.assertNotEqual(clean, tracked)
            self.assertEqual(["README.md"], paths)

            untracked = repo.path / "new.txt"
            untracked.write_text("one\n", encoding="utf-8")
            first_untracked, paths = diff_fingerprint(context)
            untracked.write_text("two\n", encoding="utf-8")
            second_untracked, _ = diff_fingerprint(context)
            self.assertNotEqual(first_untracked, second_untracked)
            self.assertIn("new.txt", paths)

    def test_linked_worktree_has_isolated_absolute_git_dir(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as parent:
            worktree = Path(parent) / "isolated"
            _support.git(repo.path, "worktree", "add", "--detach", str(worktree), "HEAD")
            primary = resolve_repo(repo.path)
            linked = resolve_repo(worktree)
            self.assertNotEqual(primary.git_dir, linked.git_dir)
            self.assertIn("worktrees", str(linked.git_dir))


class StoreTests(unittest.TestCase):
    def test_contract_is_immutable_and_files_are_private(self) -> None:
        with _support.TempRepo() as repo:
            context = resolve_repo(repo.path)
            contract, state = build_contract(
                {"goal": "Update docs", "done_when": ["Docs are current"]},
                context,
            )
            store = RunStore(context)
            store.create(contract, state)
            directory = store.run_dir(contract["run_id"])
            original = (directory / "contract.json").read_bytes()
            updated = store.save_state(contract["run_id"], state)
            self.assertEqual(original, (directory / "contract.json").read_bytes())
            self.assertEqual(1, updated["revision"])
            for name in ("contract.json", "state.json", "review.json", "events.jsonl"):
                mode = stat.S_IMODE((directory / name).stat().st_mode)
                self.assertEqual(0o600, mode, name)
            self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))

    def test_corrupt_state_is_rejected(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService()
            run = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                }
            )
            run_id = run["contract"]["run_id"]
            store = RunStore.for_workspace(repo.path)
            (store.run_dir(run_id) / "state.json").write_text("{broken", encoding="utf-8")
            with self.assertRaises(StateError):
                service.get_run({"workspace": str(repo.path), "run_id": run_id})

    def test_restart_marks_running_stage_interrupted(self) -> None:
        with _support.TempRepo() as repo:
            first = HarnessService()
            run = first.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                }
            )
            run_id = run["contract"]["run_id"]
            store = RunStore.for_workspace(repo.path)
            state = store.read_state(run_id)
            state["stages"] = {
                f"{run_id}:critic:1": {
                    "profile": "critic",
                    "lifecycle_state": "running",
                }
            }
            store.save_state(run_id, state)

            restarted = HarnessService()
            recovered = restarted.get_run(
                {"workspace": str(repo.path), "run_id": run_id}
            )
            self.assertEqual("interrupted", recovered["state"]["phase"])
            self.assertEqual(
                "interrupted",
                next(iter(recovered["state"]["stages"].values()))[
                    "lifecycle_state"
                ],
            )

    def test_dirty_worktree_is_rejected_without_explicit_acknowledgement(self) -> None:
        with _support.TempRepo() as repo:
            original = "user change\n"
            (repo.path / "README.md").write_text(original, encoding="utf-8")
            service = HarnessService()
            with self.assertRaises(InputError):
                service.create_run(
                    {
                        "workspace": str(repo.path),
                        "goal": "Update docs",
                        "done_when": ["Docs are current"],
                    }
                )
            self.assertEqual(
                original, (repo.path / "README.md").read_text(encoding="utf-8")
            )


class PolicyTests(unittest.TestCase):
    def test_policy_adds_checks_and_only_escalates_risk(self) -> None:
        with _support.TempRepo() as repo:
            config_dir = repo.path / ".codex"
            config_dir.mkdir()
            (config_dir / "agent-harness.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "rules": [
                            {
                                "paths": ["src/**/*.py", "src/*.py"],
                                "risk": "high",
                                "checks": [
                                    {
                                        "name": "unit",
                                        "argv": ["python3", "-m", "unittest"],
                                        "timeout_seconds": 120,
                                    }
                                ],
                            },
                            {"paths": ["src/*.py"], "risk": "low", "checks": []},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            frozen = validate_checks(
                [{"name": "lint", "argv": ["ruff", "check", "."]}],
                source="contract",
            )
            checks, risk, matched = plan_checks(
                repo_root=repo.path,
                frozen_checks=frozen,
                initial_risk="medium",
                changed_paths=["src/main.py"],
            )
            self.assertEqual("high", risk)
            self.assertEqual([0, 1], matched)
            self.assertEqual(
                ["git-diff-check", "lint", "unit"],
                [check["name"] for check in checks],
            )

    def test_conflicting_check_names_are_rejected(self) -> None:
        with _support.TempRepo() as repo:
            config_dir = repo.path / ".codex"
            config_dir.mkdir()
            (config_dir / "agent-harness.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "rules": [
                            {
                                "paths": ["*"],
                                "checks": [
                                    {"name": "lint", "argv": ["other-lint"]}
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            frozen = validate_checks(
                [{"name": "lint", "argv": ["lint"]}], source="contract"
            )
            with self.assertRaises(InputError):
                plan_checks(
                    repo_root=repo.path,
                    frozen_checks=frozen,
                    initial_risk="low",
                    changed_paths=["README.md"],
                )


if __name__ == "__main__":
    unittest.main()
