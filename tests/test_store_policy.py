from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import _support

from agent_harness.contract import build_contract
from agent_harness.git_repo import (
    diff_fingerprint,
    diff_stats,
    full_diff_check,
    resolve_repo,
)
from agent_harness.policy import plan_checks, validate_checks
from agent_harness.service import HarnessService
from agent_harness.store import RunStore
from agent_harness.util import InputError, StateError


class GitFingerprintTests(unittest.TestCase):
    def test_resolve_repo_supports_non_ascii_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Дмитрий"
            path.mkdir()
            _support.git(path, "init", "-b", "main")
            _support.git(path, "config", "user.name", "Agent Harness Tests")
            _support.git(path, "config", "user.email", "tests@example.invalid")
            (path / "README.md").write_text("initial\n", encoding="utf-8")
            _support.git(path, "add", "README.md")
            _support.git(path, "commit", "-m", "initial")

            context = resolve_repo(path)

            self.assertEqual(path.resolve(), context.repo_root)

    def test_diff_stats_classify_text_generated_tests_and_binary(self) -> None:
        with _support.TempRepo() as repo:
            files = {
                "src/app.py": "one\ntwo\nthree\n",
                "src/unknown.odd": "one\ntwo\n",
                "tests/test_app.py": "one\ntwo\nthree\nfour\n",
                "docs/guide.md": "one\ntwo\n",
                "composer.lock": "one\ntwo\nthree\n",
                "go.mod": "module example.com/service\ngo 1.24\n",
                "Jenkinsfile": "pipeline {}\n",
                "src/api.pb.cc": "one\ntwo\n",
                "src/api.pb.h": "one\n",
                "artifact.out": "one\ntwo\n",
                ".gitattributes": "artifact.out linguist-generated\n",
            }
            for relative, content in files.items():
                path = repo.path / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            (repo.path / "image.bin").write_bytes(b"one\x00two")

            statistics = diff_stats(resolve_repo(repo.path))

            self.assertEqual(5, statistics["production"]["total"])
            self.assertEqual(4, statistics["tests"]["total"])
            self.assertEqual(2, statistics["documentation"]["total"])
            self.assertEqual(4, statistics["configuration"]["total"])
            self.assertEqual(8, statistics["generated"]["total"])
            self.assertEqual(1, statistics["binary"]["files"])
            categories = {
                item["path"]: item["category"] for item in statistics["paths"]
            }
            self.assertEqual("generated", categories["artifact.out"])
            self.assertEqual("generated", categories["src/api.pb.cc"])
            self.assertEqual("generated", categories["src/api.pb.h"])
            self.assertEqual("configuration", categories[".gitattributes"])
            self.assertEqual("configuration", categories["go.mod"])
            self.assertEqual("configuration", categories["Jenkinsfile"])

    def test_diff_stats_count_tracked_deletions_and_rename_destination(self) -> None:
        with _support.TempRepo() as repo:
            source = repo.path / "src"
            source.mkdir()
            (source / "old.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
            (source / "remove.py").write_text("one\ntwo\n", encoding="utf-8")
            _support.git(repo.path, "add", "src")
            _support.git(repo.path, "commit", "-m", "add source")
            base_sha = resolve_repo(repo.path).head_sha
            _support.git(repo.path, "mv", "src/old.py", "src/new.py")
            (source / "remove.py").unlink()

            statistics = diff_stats(resolve_repo(repo.path), base_sha=base_sha)

            self.assertEqual(2, statistics["production"]["deleted"])
            paths = {item["path"] for item in statistics["paths"]}
            self.assertIn("src/new.py", paths)
            self.assertNotIn("src/old.py", paths)

    def test_deleted_and_renamed_generated_files_use_base_attributes(self) -> None:
        with _support.TempRepo() as repo:
            generated = repo.path / "generated"
            generated.mkdir()
            (repo.path / ".gitattributes").write_text(
                "generated/*.odd linguist-generated\n",
                encoding="utf-8",
            )
            (generated / "deleted.odd").write_text("one\ntwo\n", encoding="utf-8")
            (generated / "old.odd").write_text("one\n", encoding="utf-8")
            _support.git(repo.path, "add", ".gitattributes", "generated")
            _support.git(repo.path, "commit", "-m", "add generated fixtures")
            base_sha = resolve_repo(repo.path).head_sha
            (repo.path / ".gitattributes").unlink()
            (generated / "deleted.odd").unlink()
            _support.git(repo.path, "mv", "generated/old.odd", "renamed.odd")

            statistics = diff_stats(resolve_repo(repo.path), base_sha=base_sha)

            self.assertEqual(2, statistics["generated"]["files"])
            self.assertEqual(2, statistics["generated"]["deleted"])
            categories = {
                item["path"]: item["category"] for item in statistics["paths"]
            }
            self.assertEqual("generated", categories["generated/deleted.odd"])
            self.assertEqual("generated", categories["renamed.odd"])
            self.assertEqual("configuration", categories[".gitattributes"])

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

    def test_fingerprint_reports_committed_paths_since_base(self) -> None:
        with _support.TempRepo() as repo:
            base_sha = resolve_repo(repo.path).head_sha
            (repo.path / "README.md").write_text("committed\n", encoding="utf-8")
            _support.git(repo.path, "add", "README.md")
            _support.git(repo.path, "commit", "-m", "change")

            _fingerprint, paths = diff_fingerprint(
                resolve_repo(repo.path),
                base_sha=base_sha,
            )
            self.assertEqual(["README.md"], paths)

    def test_fingerprint_tracks_index_blob_when_worktree_is_unchanged(self) -> None:
        with _support.TempRepo() as repo:
            base = resolve_repo(repo.path).head_sha
            path = repo.path / "README.md"
            path.write_text("staged one\n", encoding="utf-8")
            _support.git(repo.path, "add", "README.md")
            path.write_text("fixed worktree\n", encoding="utf-8")
            first, _paths = diff_fingerprint(resolve_repo(repo.path), base_sha=base)

            blob_source = repo.path / "blob-source"
            blob_source.write_text("staged two \n", encoding="utf-8")
            blob = _support.git(repo.path, "hash-object", "-w", str(blob_source))
            blob_source.unlink()
            _support.git(
                repo.path,
                "update-index",
                "--cacheinfo",
                f"100644,{blob},README.md",
            )
            second, _paths = diff_fingerprint(resolve_repo(repo.path), base_sha=base)

            self.assertNotEqual(first, second)
            self.assertEqual("fixed worktree\n", path.read_text(encoding="utf-8"))

    def test_full_diff_check_covers_all_git_states_without_mutating_index(self) -> None:
        with _support.TempRepo() as repo:
            context = resolve_repo(repo.path)
            base = context.head_sha
            (repo.path / "README.md").write_text("committed bad \n", encoding="utf-8")
            _support.git(repo.path, "add", "README.md")
            _support.git(repo.path, "commit", "-m", "bad committed content")
            (repo.path / "README.md").write_text("staged bad \n", encoding="utf-8")
            _support.git(repo.path, "add", "README.md")
            (repo.path / "README.md").write_text("clean worktree\n", encoding="utf-8")
            (repo.path / "- new file.txt").write_text("untracked bad \n", encoding="utf-8")
            index_path = Path(_support.git(repo.path, "rev-parse", "--git-path", "index"))
            if not index_path.is_absolute():
                index_path = repo.path / index_path
            before = hashlib.sha256(index_path.read_bytes()).hexdigest()

            issues = full_diff_check(resolve_repo(repo.path), base_sha=base)

            after = hashlib.sha256(index_path.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            self.assertTrue(any("README.md" in item for item in issues))
            self.assertTrue(any("- new file.txt" in item for item in issues))

    def test_full_diff_check_detects_each_git_source_independently(self) -> None:
        def committed(repo: _support.TempRepo) -> str:
            (repo.path / "README.md").write_text("committed bad \n", encoding="utf-8")
            _support.git(repo.path, "add", "README.md")
            _support.git(repo.path, "commit", "-m", "committed whitespace")
            return "README.md"

        def staged(repo: _support.TempRepo) -> str:
            path = repo.path / "README.md"
            path.write_text("staged bad \n", encoding="utf-8")
            _support.git(repo.path, "add", "README.md")
            path.write_text("initial\n", encoding="utf-8")
            return "README.md"

        def untracked(repo: _support.TempRepo) -> str:
            name = "- untracked file.txt"
            (repo.path / name).write_text("untracked bad \n", encoding="utf-8")
            return name

        for name, prepare in (
            ("committed", committed),
            ("staged", staged),
            ("untracked", untracked),
        ):
            with self.subTest(name=name), _support.TempRepo() as repo:
                context = resolve_repo(repo.path)
                base = context.head_sha
                expected_path = prepare(repo)
                index_path = Path(
                    _support.git(repo.path, "rev-parse", "--git-path", "index")
                )
                if not index_path.is_absolute():
                    index_path = repo.path / index_path
                before = hashlib.sha256(index_path.read_bytes()).hexdigest()

                issues = full_diff_check(context, base_sha=base)

                after = hashlib.sha256(index_path.read_bytes()).hexdigest()
                self.assertEqual(before, after)
                self.assertEqual(1, len(issues))
                self.assertTrue(any(expected_path in issue for issue in issues))

    def test_run_can_freeze_an_ancestor_as_combined_review_base(self) -> None:
        with _support.TempRepo() as repo:
            base_sha = resolve_repo(repo.path).head_sha
            (repo.path / "README.md").write_text("committed\n", encoding="utf-8")
            _support.git(repo.path, "add", "README.md")
            _support.git(repo.path, "commit", "-m", "change")
            run = HarnessService({}).create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Review the combined result",
                    "done_when": ["Combined diff is checked"],
                    "base_sha": base_sha,
                }
            )
            self.assertEqual(base_sha, run["contract"]["base_sha"])
            with self.assertRaisesRegex(InputError, "local commit"):
                HarnessService({}).create_run(
                    {
                        "workspace": str(repo.path),
                        "goal": "Use an invalid base",
                        "done_when": ["Never starts"],
                        "base_sha": "a" * 40,
                    }
                )

    def test_linked_worktree_has_isolated_absolute_git_dir(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as parent:
            worktree = Path(parent) / "isolated"
            _support.git(repo.path, "worktree", "add", "--detach", str(worktree), "HEAD")
            primary = resolve_repo(repo.path)
            linked = resolve_repo(worktree)
            self.assertNotEqual(primary.git_dir, linked.git_dir)
            self.assertEqual(primary.git_common_dir, linked.git_common_dir)
            self.assertIn("worktrees", str(linked.git_dir))


class StoreTests(unittest.TestCase):
    def test_new_run_freezes_default_review_budget(self) -> None:
        with _support.TempRepo() as repo:
            run = HarnessService({}).create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update code",
                    "done_when": ["Code is current"],
                }
            )
            self.assertEqual(
                {
                    "expected_production_lines": {"min": 0, "max": 700},
                    "max_production_lines": 700,
                    "exception_reason": None,
                },
                run["contract"]["review_budget"],
            )
            self.assertEqual("advisory", run["contract"]["review_budget_mode"])

    def test_expected_overage_requires_reason_but_not_high_risk(
        self,
    ) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            with self.assertRaisesRegex(InputError, "requires exception_reason"):
                service.create_run(
                    {
                        "workspace": str(repo.path),
                        "goal": "Upgrade runtime",
                        "done_when": ["Runtime works"],
                        "review_budget": {
                            "expected_production_lines": {"min": 800, "max": 900}
                        },
                    }
                )
            run = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Upgrade runtime",
                    "done_when": ["Runtime works"],
                    "risk": "medium",
                    "review_budget": {
                        "expected_production_lines": {"min": 800, "max": 900},
                        "exception_reason": "Intermediate states do not build",
                    },
                }
            )
            self.assertEqual(
                "Intermediate states do not build",
                run["contract"]["review_budget"]["exception_reason"],
            )

    def test_legacy_run_measurement_is_report_only_and_read_only(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            run = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update code",
                    "done_when": ["Code is current"],
                }
            )
            run_id = run["contract"]["run_id"]
            store = RunStore.for_workspace(repo.path)
            contract_path = store.run_dir(run_id) / "contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract.pop("review_budget")
            contract.pop("review_budget_mode")
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            (repo.path / "app.py").write_text("changed\n", encoding="utf-8")
            before = (store.run_dir(run_id) / "state.json").read_bytes()

            measured = service.measure_diff(
                {"workspace": str(repo.path), "run_id": run_id}
            )

            self.assertEqual("legacy_report_only", measured["budget_status"])
            self.assertEqual(1, measured["diff_stats"]["production"]["total"])
            self.assertEqual(
                before,
                (store.run_dir(run_id) / "state.json").read_bytes(),
            )

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
            if os.name != "nt":
                for name in (
                    "contract.json",
                    "state.json",
                    "review.json",
                    "events.jsonl",
                ):
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

    def test_completed_stage_clears_stale_interruption(self) -> None:
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
            state = store.read_state(run_id)
            state["phase"] = "reviewing"
            stage_id = f"{run_id}:critic:1"
            state["terminal"] = {
                "status": "interrupted",
                "source": "stage_recovery",
                "stage_ids": [stage_id],
            }
            state["stages"] = {
                stage_id: {
                    "profile": "critic",
                    "lifecycle_state": "completed",
                }
            }
            store.save_state(run_id, state)

            recovered = service.get_run(
                {"workspace": str(repo.path), "run_id": run_id}
            )

            self.assertIsNone(recovered["state"]["terminal"])

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
