from __future__ import annotations

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _support

from agent_harness.campaign import CampaignStore
from agent_harness.git_repo import resolve_repo
from agent_harness.mcp_server import McpServer
from agent_harness.service import HarnessService
from agent_harness.specification import MAX_DOCUMENT_BYTES
from agent_harness.store import RunStore
from agent_harness.util import InputError, StateError


SPEC = {"kind": "harness", "change_id": "add-feature", "readiness": "ready"}


def prepare_draft(repo: Path, tasks: tuple[str, ...] = ()) -> Path:
    exclude = Path(
        _support.git(
            repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"
        )
    )
    exclude.write_text(
        exclude.read_text(encoding="utf-8") + "\n/.agent-harness/specs/\n",
        encoding="utf-8",
    )
    draft = repo / ".agent-harness/specs/add-feature"
    draft.mkdir(parents=True)
    (draft / "spec.md").write_text("Approved common behavior\n", encoding="utf-8")
    for task_id in tasks:
        target = draft / "tasks" / f"{task_id}.md"
        target.parent.mkdir(exist_ok=True)
        target.write_text(f"Approved behavior for {task_id}\n", encoding="utf-8")
    return draft


def task(task_id: str, workspace: Path | None = None) -> dict:
    definition = {
        "id": task_id,
        "title": task_id,
        "goal": f"Complete {task_id}",
        "done_when": [f"{task_id} works"],
        "kind": "implementation",
        "dependencies": [],
    }
    if workspace is not None:
        definition["workspace"] = str(workspace)
    return definition


def campaign_args(repo: Path, tasks: list[dict], spec: dict | None = None) -> dict:
    return {
        "workspace": str(repo),
        "title": "Native specification campaign",
        "goal": "Complete the feature",
        "done_when": ["All tasks are complete"],
        "source": {"kind": "local", "ref": "native-spec-test"},
        "risk": "medium",
        "mode": "delivery",
        "tasks": tasks,
        "spec": SPEC if spec is None else spec,
    }


def run_args(repo: Path, spec: dict | None = None) -> dict:
    return {
        "workspace": str(repo),
        "goal": "Update the README",
        "done_when": ["README documents the behavior"],
        "spec": SPEC if spec is None else spec,
    }


def planned_checks(service: HarnessService, repo: Path, run_id: str) -> None:
    plan = service.plan_checks({"workspace": str(repo), "run_id": run_id})
    for check in plan["checks"]:
        completed = subprocess.run(
            check["argv"],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=check["timeout_seconds"],
            check=False,
        )
        service.record_check(
            {
                "workspace": str(repo),
                "run_id": run_id,
                "check_name": check["name"],
                "exit_code": completed.returncode,
                "duration_ms": 1,
                "summary": "passed" if completed.returncode == 0 else "failed",
            }
        )
        if completed.returncode:
            raise AssertionError(f"planned check {check['name']} failed")


class NativeSpecificationTests(unittest.TestCase):
    def test_storage_edges_case_colliding_tasks_fail_before_state_creation(self) -> None:
        with _support.TempRepo() as repo:
            prepare_draft(repo.path, ("T-1", "t-1"))
            with self.assertRaisesRegex(InputError, "task set"):
                HarnessService({}).create_campaign(
                    campaign_args(repo.path, [task("T-1"), task("t-1")])
                )
            self.assertEqual([], list(CampaignStore.for_workspace(repo.path).list_campaign_ids()))

    def test_storage_edges_campaign_survives_original_worktree_removal(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "origin-worktree"
            _support.git(repo.path, "worktree", "add", "--detach", str(worktree), "HEAD")
            prepare_draft(worktree, ("T-1",))
            created = HarnessService({}).create_campaign(
                campaign_args(worktree, [task("T-1", repo.path)])
            )
            _support.git(repo.path, "worktree", "remove", "--force", str(worktree))
            loaded = HarnessService({}).get_campaign({
                "workspace": str(repo.path), "campaign_id": created["contract"]["campaign_id"],
            })
            self.assertEqual(created["spec_context"], loaded["spec_context"])

    def test_approved_snapshots_survive_stricter_draft_policy(self) -> None:
        with _support.TempRepo() as repo:
            draft = prepare_draft(repo.path, ("T-1",))
            service = HarnessService({})
            campaign = service.create_campaign(campaign_args(repo.path, [task("T-1")]))
            campaign_id = campaign["contract"]["campaign_id"]
            run = service.create_run(run_args(repo.path))
            run_id = run["contract"]["run_id"]
            with patch("agent_harness.specification.sanitize_text", return_value="new policy rejection"):
                restarted = HarnessService({})
                self.assertEqual(run["spec_context"], restarted.get_run({
                    "workspace": str(repo.path), "run_id": run_id,
                })["spec_context"])
                self.assertEqual(campaign["spec_context"], restarted.get_campaign({
                    "workspace": str(repo.path), "campaign_id": campaign_id,
                })["spec_context"])
                with self.assertRaisesRegex(InputError, "possible credentials.*spec.md"):
                    restarted.create_run(run_args(repo.path))
                with self.assertRaisesRegex(InputError, "possible credentials.*spec.md"):
                    restarted.create_campaign(campaign_args(repo.path, [task("T-1")]))
            (draft / "tasks/T-1.md").write_bytes(b"invalid\x00draft")
            with self.assertRaisesRegex(InputError, "contains NUL: tasks/T-1.md"):
                service.create_campaign(campaign_args(repo.path, [task("T-1")]))

    def test_native_spec_roundtrip_through_mcp_and_new_approval_revision(self) -> None:
        with _support.TempRepo() as repo:
            draft = prepare_draft(repo.path, ("T-1",))
            server = McpServer(HarnessService({}))
            result = server.call_tool("create_campaign", campaign_args(repo.path, [task("T-1")]))
            self.assertFalse(result["isError"])
            first = result["structuredContent"]
            (draft / "tasks/T-1.md").write_text("Newly approved task boundary\n", encoding="utf-8")
            second = server.call_tool("create_campaign", campaign_args(repo.path, [task("T-1")]))
            self.assertFalse(second["isError"])
            self.assertNotEqual(
                first["spec_context"]["revision"],
                second["structuredContent"]["spec_context"]["revision"],
            )
            original = server.call_tool("get_campaign", {
                "workspace": str(repo.path), "campaign_id": first["contract"]["campaign_id"],
            })
            self.assertFalse(original["isError"])
            self.assertEqual(first["spec_context"], original["structuredContent"]["spec_context"])
            self.assertEqual(
                "Approved behavior for T-1\n",
                Path(original["structuredContent"]["spec_context"]["documents"]["tasks/T-1.md"]).read_text(),
            )
            direct = server.call_tool("create_run", run_args(repo.path))
            self.assertFalse(direct["isError"])
            self.assertEqual({"spec.md"}, set(direct["structuredContent"]["spec_context"]["documents"]))

    def test_standalone_snapshot_survives_draft_change_restart_and_full_cycle(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            draft = prepare_draft(repo.path)
            fake = _support.make_fake_claude(Path(directory))
            environment = _support.fake_environment(fake)
            service = HarnessService(environment)
            created = service.create_run(run_args(repo.path))
            run_id = created["contract"]["run_id"]
            context = created["spec_context"]
            snapshot = Path(context["documents"]["spec.md"])
            self.assertEqual("approved_snapshot", context["authority"])
            self.assertEqual("ready", context["readiness"])
            self.assertEqual("Approved common behavior\n", snapshot.read_text())
            self.assertEqual(
                resolve_repo(repo.path).git_dir,
                snapshot.parents[4],
            )
            self.assertFalse(created["contract"]["initial_worktree"]["dirty"])
            self.assertNotIn("Approved common behavior", str(created["contract"]))

            (draft / "spec.md").write_text("Unapproved later draft\n", encoding="utf-8")
            (draft / "spec.md").unlink()
            restarted = HarnessService(environment)
            loaded = restarted.get_run({"workspace": str(repo.path), "run_id": run_id})
            self.assertEqual(context, loaded["spec_context"])
            self.assertEqual("Approved common behavior\n", snapshot.read_text())
            (repo.path / "README.md").write_text("Updated behavior\n", encoding="utf-8")
            planned_checks(restarted, repo.path, run_id)
            stage = restarted.start_stage(
                {"workspace": str(repo.path), "run_id": run_id, "profile": "critic"}
            )
            _support.wait_until(
                lambda: restarted.poll_stage(
                    {
                        "workspace": str(repo.path),
                        "run_id": run_id,
                        "stage_id": stage["stage_id"],
                        "wait_seconds": 0.1,
                    }
                ).get("terminal")
            )
            _support.wait_until(
                lambda: restarted.get_run(
                    {"workspace": str(repo.path), "run_id": run_id}
                )["review"]["review"]
            )
            restarted.record_review_resolution(
                {"workspace": str(repo.path), "run_id": run_id, "resolutions": []}
            )
            finished = restarted.finish_run(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "status": "complete",
                    "summary": "Local checks and fake review passed",
                }
            )
            self.assertEqual("complete", finished["phase"])

    def test_campaign_scopes_each_run_to_common_and_own_task_across_worktrees(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            prepare_draft(repo.path, ("T-1", "T-2"))
            worktree = Path(directory) / "second-worktree"
            _support.git(repo.path, "worktree", "add", "--detach", str(worktree), "HEAD")
            service = HarnessService({})
            created = service.create_campaign(
                campaign_args(repo.path, [task("T-1"), task("T-2", worktree)])
            )
            campaign_id = created["contract"]["campaign_id"]
            campaign_context = created["spec_context"]
            self.assertEqual(
                {"spec.md", "tasks/T-1.md", "tasks/T-2.md"},
                set(campaign_context["documents"]),
            )
            common = resolve_repo(repo.path).git_common_dir
            self.assertEqual(common, Path(campaign_context["documents"]["spec.md"]).parents[4])
            for task_id, workspace in (("T-1", repo.path), ("T-2", worktree)):
                service.record_campaign_task(
                    {
                        "workspace": str(workspace),
                        "campaign_id": campaign_id,
                        "task_id": task_id,
                        "status": "in_progress",
                    }
                )
                linked = service.create_run(
                    {
                        "workspace": str(workspace),
                        "campaign": {
                            "workspace": str(repo.path),
                            "campaign_id": campaign_id,
                            "task_id": task_id,
                        },
                    }
                )
                context = linked["spec_context"]
                self.assertEqual({"spec.md", f"tasks/{task_id}.md"}, set(context["documents"]))
                self.assertEqual("approved_snapshot", context["authority"])
                self.assertEqual(
                    resolve_repo(workspace).git_dir,
                    Path(context["documents"]["spec.md"]).parents[4],
                )
                refs = {item["ref"] for item in linked["contract"]["contract_refs"]}
                self.assertEqual(
                    {"harness:add-feature/spec.md", f"harness:add-feature/tasks/{task_id}.md"},
                    refs,
                )
                self.assertEqual(
                    context,
                    HarnessService({}).get_run(
                        {"workspace": str(workspace), "run_id": linked["contract"]["run_id"]}
                    )["spec_context"],
                )
            self.assertEqual(
                campaign_context,
                HarnessService({}).get_campaign(
                    {"workspace": str(worktree), "campaign_id": campaign_id}
                )["spec_context"],
            )

    def test_campaign_can_scope_task_in_another_repository(self) -> None:
        with _support.TempRepo() as repo, _support.TempRepo() as other:
            prepare_draft(repo.path, ("T-1", "T-2"))
            service = HarnessService({})
            campaign_id = service.create_campaign(
                campaign_args(repo.path, [task("T-1"), task("T-2", other.path)])
            )["contract"]["campaign_id"]
            service.record_campaign_task(
                {
                    "workspace": str(repo.path),
                    "campaign_id": campaign_id,
                    "task_id": "T-2",
                    "status": "in_progress",
                }
            )
            run = service.create_run(
                {
                    "workspace": str(other.path),
                    "campaign": {
                        "workspace": str(repo.path),
                        "campaign_id": campaign_id,
                        "task_id": "T-2",
                    },
                }
            )
            context = run["spec_context"]
            self.assertEqual({"spec.md", "tasks/T-2.md"}, set(context["documents"]))
            self.assertEqual(
                resolve_repo(other.path).git_dir,
                Path(context["documents"]["spec.md"]).parents[4],
            )

    def test_missing_task_and_analysis_required_are_rejected(self) -> None:
        with _support.TempRepo() as repo:
            prepare_draft(repo.path)
            with self.assertRaises(InputError):
                HarnessService({}).create_campaign(
                    campaign_args(repo.path, [task("T-1")])
                )
            with self.assertRaises(InputError):
                HarnessService({}).create_run(
                    run_args(repo.path, {**SPEC, "readiness": "analysis_required"})
                )
        with _support.TempRepo() as repo:
            prepare_draft(repo.path, ("T-1",))
            with self.assertRaises(InputError):
                HarnessService({}).create_campaign(
                    campaign_args(
                        repo.path,
                        [task("T-1")],
                        {**SPEC, "readiness": "analysis_required"},
                    )
                )

    def test_analysis_required_campaign_allows_analysis_only(self) -> None:
        with _support.TempRepo() as repo:
            prepare_draft(repo.path, ("T-1",))
            definition = task("T-1")
            definition["kind"] = "analysis"
            result = HarnessService({}).create_campaign(
                campaign_args(
                    repo.path,
                    [definition],
                    {**SPEC, "readiness": "analysis_required"},
                )
            )
            self.assertEqual("analysis_required", result["spec_context"]["readiness"])

    def test_linked_override_and_conflicting_references_are_rejected(self) -> None:
        with _support.TempRepo() as repo:
            prepare_draft(repo.path, ("T-1",))
            service = HarnessService({})
            campaign_id = service.create_campaign(
                campaign_args(repo.path, [task("T-1")])
            )["contract"]["campaign_id"]
            service.record_campaign_task(
                {
                    "workspace": str(repo.path),
                    "campaign_id": campaign_id,
                    "task_id": "T-1",
                    "status": "in_progress",
                }
            )
            linked = {
                "workspace": str(repo.path),
                "campaign": {
                    "workspace": str(repo.path),
                    "campaign_id": campaign_id,
                    "task_id": "T-1",
                },
            }
            with self.assertRaises(InputError):
                service.create_run({**linked, "spec": SPEC})
            with self.assertRaises(InputError):
                service.create_run(
                    {
                        **linked,
                        "contract_refs": [
                            {"ref": "harness:add-feature/spec.md", "revision": "wrong"}
                        ],
                    }
                )
            with self.assertRaises(InputError):
                service.create_run(
                    {
                        **linked,
                        "contract_refs": [
                            {"ref": "harness:add-feature/tasks/T-1.md", "revision": "wrong"}
                        ],
                    }
                )

    def test_invalid_draft_paths_and_contents_are_rejected(self) -> None:
        cases = (
            ("missing", lambda draft: (draft / "spec.md").unlink()),
            ("empty", lambda draft: (draft / "spec.md").write_text(" \n", encoding="utf-8")),
            ("nul", lambda draft: (draft / "spec.md").write_bytes(b"text\x00more")),
            ("non-utf8", lambda draft: (draft / "spec.md").write_bytes(b"\xff")),
            (
                "oversize",
                lambda draft: (draft / "spec.md").write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1)),
            ),
            (
                "credential",
                lambda draft: (draft / "spec.md").write_text(
                    "api_" + "key=some-secret-value\n", encoding="utf-8"
                ),
            ),
            (
                "url-credential",
                lambda draft: (draft / "spec.md").write_text(
                    "https://" + "user:password@example.invalid/path\n", encoding="utf-8"
                ),
            ),
        )
        for name, mutation in cases:
            with self.subTest(name=name), _support.TempRepo() as repo:
                draft = prepare_draft(repo.path)
                mutation(draft)
                with self.assertRaisesRegex(InputError, "spec.md"):
                    HarnessService({}).create_run(run_args(repo.path))

    def test_symlink_and_traversal_are_rejected(self) -> None:
        with _support.TempRepo() as repo:
            draft = prepare_draft(repo.path)
            target = draft / "actual.md"
            target.write_text("Actual document\n", encoding="utf-8")
            (draft / "spec.md").unlink()
            (draft / "spec.md").symlink_to(target)
            with self.assertRaises(InputError):
                HarnessService({}).create_run(run_args(repo.path))
        with _support.TempRepo() as repo:
            prepare_draft(repo.path)
            for change_id in ("../outside", "a/b", "UPPER", "."):
                with self.subTest(change_id=change_id), self.assertRaises(InputError):
                    HarnessService({}).create_run(run_args(repo.path, {**SPEC, "change_id": change_id}))

    def test_draft_must_be_ignored_and_untracked(self) -> None:
        with _support.TempRepo() as repo:
            draft = prepare_draft(repo.path)
            exclude = Path(
                _support.git(
                    repo.path,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-path",
                    "info/exclude",
                )
            )
            exclude.write_text("", encoding="utf-8")
            with self.assertRaises(InputError):
                HarnessService({}).create_run(run_args(repo.path))
            (repo.path / ".gitignore").write_text(
                "/.agent-harness/specs/\n", encoding="utf-8"
            )
            _support.git(repo.path, "add", ".gitignore")
            _support.git(repo.path, "commit", "-m", "ignore drafts")
            self.assertIn("spec_context", HarnessService({}).create_run(run_args(repo.path)))
        with _support.TempRepo() as repo:
            draft = prepare_draft(repo.path)
            _support.git(repo.path, "add", "-f", str(draft / "spec.md"))
            with self.assertRaises(InputError):
                HarnessService({}).create_run(run_args(repo.path))

    def test_snapshot_permissions_and_corruption_block_progress(self) -> None:
        with _support.TempRepo() as repo:
            draft = prepare_draft(repo.path, ("T-1",))
            service = HarnessService({})
            campaign = service.create_campaign(campaign_args(repo.path, [task("T-1")]))
            campaign_id = campaign["contract"]["campaign_id"]
            campaign_snapshot = CampaignStore.for_workspace(repo.path).spec_dir(campaign_id)
            for path in (campaign_snapshot, *campaign_snapshot.rglob("*")):
                self.assertEqual(
                    0o700 if path.is_dir() else 0o600,
                    stat.S_IMODE(path.stat().st_mode),
                )
            (draft / "spec.md").unlink()
            service.record_campaign_task(
                {
                    "workspace": str(repo.path),
                    "campaign_id": campaign_id,
                    "task_id": "T-1",
                    "status": "in_progress",
                }
            )
            linked = service.create_run(
                {
                    "workspace": str(repo.path),
                    "campaign": {
                        "workspace": str(repo.path),
                        "campaign_id": campaign_id,
                        "task_id": "T-1",
                    },
                }
            )
            run_id = linked["contract"]["run_id"]
            run_snapshot = RunStore.for_workspace(repo.path).run_dir(run_id) / "spec"
            for path in (run_snapshot, *run_snapshot.rglob("*")):
                self.assertEqual(
                    0o700 if path.is_dir() else 0o600,
                    stat.S_IMODE(path.stat().st_mode),
                )
            (run_snapshot / "tasks/T-1.md").write_text("Corrupt\n", encoding="utf-8")
            with self.assertRaises(StateError):
                service.get_run({"workspace": str(repo.path), "run_id": run_id})
            with self.assertRaises(StateError):
                service.plan_checks({"workspace": str(repo.path), "run_id": run_id})
            with self.assertRaises(StateError):
                service.record_check({
                    "workspace": str(repo.path), "run_id": run_id,
                    "check_name": "git-diff-check", "exit_code": 0, "duration_ms": 1,
                })
            with self.assertRaises(StateError):
                service.start_stage({"workspace": str(repo.path), "run_id": run_id, "profile": "critic"})
            with self.assertRaises(StateError):
                service.finish_run({"workspace": str(repo.path), "run_id": run_id, "status": "complete"})
            (campaign_snapshot / "spec.md").write_text("Corrupt\n", encoding="utf-8")
            with self.assertRaises(StateError):
                service.get_campaign({"workspace": str(repo.path), "campaign_id": campaign_id})


if __name__ == "__main__":
    unittest.main()
