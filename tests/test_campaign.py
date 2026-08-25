from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _support

from agent_harness.campaign import CampaignStore
from agent_harness.git_repo import diff_fingerprint, resolve_repo
from agent_harness.service import HarnessService
from agent_harness.store import RunStore
from agent_harness.util import InputError, StateError


def task(
    task_id: str,
    *,
    kind: str = "analysis",
    dependencies: list[str] | None = None,
) -> dict:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "goal": f"Complete {task_id}",
        "done_when": [f"{task_id} is complete"],
        "kind": kind,
        "dependencies": dependencies or [],
    }


def campaign_arguments(
    repo: _support.TempRepo,
    *,
    mode: str = "delivery",
    tasks: list[dict] | None = None,
    with_spec: bool = False,
) -> dict:
    if with_spec:
        _support.prepare_openspec_change(repo, commit=True)
    arguments = {
        "workspace": str(repo.path),
        "title": "Epic campaign",
        "goal": "Deliver the epic",
        "done_when": ["All campaign tasks are complete"],
        "source": {"kind": "jira", "ref": "DEMO-1"},
        "risk": "medium",
        "mode": mode,
        "tasks": tasks or [task("T-1")],
    }
    if mode == "replay":
        arguments["cutoff_at"] = "2026-01-01T00:00:00Z"
    if with_spec:
        arguments["spec"] = {"kind": "openspec", "change_id": "add-feature"}
    return arguments


def prepare_openspec_change(
    repo: _support.TempRepo,
    change_id: str = "add-feature",
) -> None:
    _support.prepare_openspec_change(repo, change_id)


def complete_run(repo: _support.TempRepo | Path, run_id: str) -> None:
    workspace = repo.path if isinstance(repo, _support.TempRepo) else repo
    run_store = RunStore.for_workspace(workspace)
    run_contract = run_store.read_contract(run_id)
    run_state = run_store.read_state(run_id)
    fingerprint, changed_paths = diff_fingerprint(
        resolve_repo(workspace),
        base_sha=run_contract["base_sha"],
    )
    run_state["diff_fingerprint"] = fingerprint
    run_state["changed_paths"] = changed_paths
    run_state["phase"] = "complete"
    run_state["terminal"] = {"status": "complete"}
    run_store.save_state(run_id, run_state)


class CampaignTests(unittest.TestCase):
    def test_bundled_openspec_schema_has_declared_templates(self) -> None:
        root = (
            _support.PLUGIN_ROOT
            / "skills/epic-workflow/assets/openspec-schema/agent-harness"
        )
        schema = (root / "schema.yaml").read_text(encoding="utf-8")
        for artifact in ("proposal", "specs", "design", "tasks"):
            self.assertIn(f"- id: {artifact}\n", schema)
        for template in ("proposal.md", "spec.md", "design.md", "tasks.md"):
            self.assertTrue((root / "templates" / template).is_file(), template)

    def test_openspec_change_is_fingerprinted_and_allows_its_dirty_files(self) -> None:
        with _support.TempRepo() as repo:
            prepare_openspec_change(repo)
            arguments = campaign_arguments(repo)
            arguments["spec"] = {
                "kind": "openspec",
                "change_id": "add-feature",
            }
            first = HarnessService({}).create_campaign(arguments)
            reference = first["contract"]["spec"]
            self.assertEqual("openspec", reference["kind"])
            self.assertEqual("openspec/changes/add-feature", reference["path"])
            self.assertEqual(64, len(reference["sha256"]))
            self.assertTrue(first["contract"]["initial_worktree"]["dirty"])
            self.assertNotIn("Need it", str(first["contract"]))

            proposal = repo.path / reference["path"] / "proposal.md"
            proposal.write_text(
                proposal.read_text(encoding="utf-8").replace(
                    "Need it.", "Changed."
                ),
                encoding="utf-8",
            )
            second = HarnessService({}).create_campaign(arguments)
            self.assertNotEqual(
                reference["sha256"], second["contract"]["spec"]["sha256"]
            )

    def test_openspec_fingerprint_ignores_only_checkbox_progress(self) -> None:
        with _support.TempRepo() as repo:
            prepare_openspec_change(repo)
            arguments = campaign_arguments(repo)
            arguments["spec"] = {
                "kind": "openspec",
                "change_id": "add-feature",
            }
            original = HarnessService({}).create_campaign(arguments)
            tasks = repo.path / "openspec/changes/add-feature/tasks.md"
            tasks.write_text(
                tasks.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
                encoding="utf-8",
            )
            progressed = HarnessService({}).create_campaign(arguments)
            self.assertEqual(
                original["contract"]["spec"]["sha256"],
                progressed["contract"]["spec"]["sha256"],
            )

    def test_semantic_openspec_change_blocks_task_progress(self) -> None:
        with _support.TempRepo() as repo:
            prepare_openspec_change(repo)
            arguments = campaign_arguments(repo)
            arguments["spec"] = {
                "kind": "openspec",
                "change_id": "add-feature",
            }
            campaign = HarnessService({}).create_campaign(arguments)
            proposal = repo.path / "openspec/changes/add-feature/proposal.md"
            proposal.write_text(
                proposal.read_text(encoding="utf-8").replace(
                    "Need it.", "Changed meaning."
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(StateError, "approved campaign specification"):
                HarnessService({}).record_campaign_task(
                    {
                        "workspace": str(repo.path),
                        "campaign_id": campaign["contract"]["campaign_id"],
                        "task_id": "T-1",
                        "status": "in_progress",
                    }
                )

    def test_archived_openspec_change_can_be_sealed_after_finalizer(
        self,
    ) -> None:
        with _support.TempRepo() as repo:
            prepare_openspec_change(repo)
            service = HarnessService({})
            arguments = campaign_arguments(
                repo,
                tasks=[
                    task("T-1"),
                    {
                        **task(
                            "openspec-finalize",
                            kind="implementation",
                            dependencies=["T-1"],
                        ),
                        "role": "finalizer",
                    },
                ],
            )
            arguments["spec"] = {
                "kind": "openspec",
                "change_id": "add-feature",
            }
            campaign = service.create_campaign(arguments)
            common = {
                "workspace": str(repo.path),
                "campaign_id": campaign["contract"]["campaign_id"],
            }
            service.record_campaign_task(
                {**common, "task_id": "T-1", "status": "complete"}
            )
            tasks = repo.path / "openspec/changes/add-feature/tasks.md"
            tasks.write_text(
                tasks.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
                encoding="utf-8",
            )
            finalizer = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Finalize OpenSpec",
                    "done_when": ["The approved change is archived"],
                    "allow_dirty": True,
                }
            )
            service.record_campaign_task(
                {
                    **common,
                    "task_id": "openspec-finalize",
                    "status": "in_progress",
                }
            )
            archive = repo.path / "openspec/changes/archive/2026-08-24-add-feature"
            archive.parent.mkdir()
            tasks.parent.rename(archive)
            complete_run(repo, finalizer["contract"]["run_id"])
            service.record_campaign_task(
                {
                    **common,
                    "task_id": "openspec-finalize",
                    "status": "complete",
                    "run_workspace": str(repo.path),
                    "run_id": finalizer["contract"]["run_id"],
                }
            )
            sealed = service.seal_campaign_candidate(
                {**common, "summary": "candidate"}
            )
            self.assertEqual(2, sealed["candidate"]["task_count"])

    def test_openspec_change_does_not_allow_unrelated_dirty_files(self) -> None:
        with _support.TempRepo() as repo:
            prepare_openspec_change(repo)
            (repo.path / "README.md").write_text("user edit\n", encoding="utf-8")
            arguments = campaign_arguments(repo)
            arguments["spec"] = {
                "kind": "openspec",
                "change_id": "add-feature",
            }
            with self.assertRaisesRegex(InputError, "outside the frozen OpenSpec"):
                HarnessService({}).create_campaign(arguments)

    def test_openspec_change_requires_initialized_complete_change(self) -> None:
        with _support.TempRepo() as repo:
            arguments = campaign_arguments(repo)
            arguments["spec"] = {
                "kind": "openspec",
                "change_id": "add-feature",
            }
            with self.assertRaisesRegex(InputError, "already be initialized"):
                HarnessService({}).create_campaign(arguments)

        with _support.TempRepo() as repo:
            prepare_openspec_change(repo)
            (repo.path / "openspec/changes/add-feature/tasks.md").unlink()
            arguments = campaign_arguments(repo)
            arguments["spec"] = {
                "kind": "openspec",
                "change_id": "add-feature",
            }
            with self.assertRaisesRegex(InputError, "missing:.*tasks.md"):
                HarnessService({}).create_campaign(arguments)

    def test_openspec_change_rejects_symlinks(self) -> None:
        with _support.TempRepo() as repo:
            prepare_openspec_change(repo)
            proposal = repo.path / "openspec/changes/add-feature/proposal.md"
            proposal.unlink()
            proposal.symlink_to(repo.path / "README.md")
            arguments = campaign_arguments(repo)
            arguments["spec"] = {
                "kind": "openspec",
                "change_id": "add-feature",
            }
            with self.assertRaisesRegex(InputError, "must not contain symlinks"):
                HarnessService({}).create_campaign(arguments)

        with _support.TempRepo() as repo, _support.TempRepo() as external:
            prepare_openspec_change(external)
            (repo.path / "openspec").symlink_to(
                external.path / "openspec",
                target_is_directory=True,
            )
            arguments = campaign_arguments(repo)
            arguments["spec"] = {
                "kind": "openspec",
                "change_id": "add-feature",
            }
            with self.assertRaisesRegex(InputError, "must not be symlinks"):
                HarnessService({}).create_campaign(arguments)

    def test_replay_rejects_openspec_change(self) -> None:
        with _support.TempRepo() as repo:
            prepare_openspec_change(repo)
            arguments = campaign_arguments(repo, mode="replay")
            arguments["spec"] = {
                "kind": "openspec",
                "change_id": "add-feature",
            }
            with self.assertRaisesRegex(InputError, "delivery campaigns"):
                HarnessService({}).create_campaign(arguments)

    def test_contract_is_immutable_and_files_are_private(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            created = service.create_campaign(campaign_arguments(repo))
            campaign_id = created["contract"]["campaign_id"]
            store = CampaignStore.for_workspace(repo.path)
            directory = store.campaign_dir(campaign_id)
            original = (directory / "contract.json").read_bytes()

            service.record_campaign_task(
                {
                    "workspace": str(repo.path),
                    "campaign_id": campaign_id,
                    "task_id": "T-1",
                    "status": "complete",
                    "summary": "done",
                }
            )

            self.assertEqual(original, (directory / "contract.json").read_bytes())
            for name in (
                "contract.json",
                "state.json",
                "comparison.json",
                "events.jsonl",
            ):
                self.assertEqual(
                    0o600,
                    stat.S_IMODE((directory / name).stat().st_mode),
                    name,
                )
            self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))

    def test_dependencies_must_complete_in_order(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            created = service.create_campaign(
                campaign_arguments(
                    repo,
                    tasks=[task("T-1"), task("T-2", dependencies=["T-1"])],
                )
            )
            campaign_id = created["contract"]["campaign_id"]
            common = {"workspace": str(repo.path), "campaign_id": campaign_id}
            with self.assertRaises(StateError):
                service.record_campaign_task(
                    {**common, "task_id": "T-2", "status": "in_progress"}
                )
            service.record_campaign_task(
                {**common, "task_id": "T-1", "status": "complete"}
            )
            started = service.record_campaign_task(
                {**common, "task_id": "T-2", "status": "in_progress"}
            )
            self.assertEqual("in_progress", started["task"]["status"])

    def test_needs_human_requires_a_real_blocking_intervention(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            campaign = service.create_campaign(campaign_arguments(repo))
            common = {
                "workspace": str(repo.path),
                "campaign_id": campaign["contract"]["campaign_id"],
                "task_id": "T-1",
            }
            with self.assertRaisesRegex(StateError, "requires an unresolved"):
                service.record_campaign_task(
                    {**common, "status": "needs_human"}
                )
            service.record_campaign_intervention(
                {
                    "workspace": str(repo.path),
                    "campaign_id": common["campaign_id"],
                    "intervention_id": "I-1",
                    "kind": "external_unblock",
                    "blocking": True,
                    "resolved": False,
                    "reason": "Access to a required registry needs user action",
                }
            )
            result = service.record_campaign_task(
                {**common, "status": "needs_human"}
            )
            self.assertEqual("needs_human", result["task"]["status"])
            state = service.get_campaign(
                {
                    "workspace": str(repo.path),
                    "campaign_id": common["campaign_id"],
                }
            )["state"]
            self.assertEqual(1, state["task_transition_counts"]["needs_human"])
            finished = service.finish_campaign(
                {
                    "workspace": str(repo.path),
                    "campaign_id": common["campaign_id"],
                    "status": "needs_human",
                    "summary": "Registry access needs user action",
                }
            )
            self.assertEqual("needs_human", finished["phase"])

    def test_campaign_needs_human_requires_a_real_blocker(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            campaign = service.create_campaign(campaign_arguments(repo))
            with self.assertRaisesRegex(StateError, "requires an unresolved"):
                service.finish_campaign(
                    {
                        "workspace": str(repo.path),
                        "campaign_id": campaign["contract"]["campaign_id"],
                        "status": "needs_human",
                        "summary": "An operational command failed",
                    }
                )

    def test_delivery_campaign_requires_openspec_for_high_risk_or_multiple_tasks(
        self,
    ) -> None:
        with _support.TempRepo() as repo:
            arguments = campaign_arguments(
                repo,
                tasks=[
                    task("T-1", kind="implementation"),
                    task("T-2", kind="implementation"),
                ],
            )
            with self.assertRaisesRegex(InputError, "OpenSpec"):
                HarnessService({}).create_campaign(arguments)
        with _support.TempRepo() as repo:
            arguments = campaign_arguments(
                repo,
                tasks=[task("T-1", kind="implementation")],
            )
            arguments["risk"] = "high"
            with self.assertRaisesRegex(InputError, "OpenSpec"):
                HarnessService({}).create_campaign(arguments)

    def test_campaign_run_inherits_risk_and_chains_a_committed_base(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            arguments = campaign_arguments(
                repo,
                tasks=[
                    task("T-1", kind="implementation"),
                    {
                        **task(
                            "T-2",
                            kind="implementation",
                            dependencies=["T-1"],
                        ),
                        "base_from_task": "T-1",
                    },
                ],
                with_spec=True,
            )
            arguments["risk"] = "high"
            campaign = service.create_campaign(arguments)
            campaign_id = campaign["contract"]["campaign_id"]
            parent = {
                "workspace": str(repo.path),
                "campaign_id": campaign_id,
                "task_id": "T-1",
            }
            service.record_campaign_task(
                {
                    "workspace": str(repo.path),
                    "campaign_id": campaign_id,
                    "task_id": "T-1",
                    "status": "in_progress",
                }
            )
            first = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Implement T-1",
                    "done_when": ["T-1 works"],
                    "risk": "low",
                    "campaign": parent,
                }
            )
            self.assertEqual("high", first["contract"]["risk"])
            (repo.path / "README.md").write_text("T-1\n", encoding="utf-8")
            complete_run(repo, first["contract"]["run_id"])
            with self.assertRaisesRegex(StateError, "clean atomic commit"):
                service.record_campaign_task(
                    {
                        "workspace": str(repo.path),
                        "campaign_id": campaign_id,
                        "task_id": "T-1",
                        "status": "complete",
                        "run_workspace": str(repo.path),
                        "run_id": first["contract"]["run_id"],
                    }
                )
            _support.git(repo.path, "add", "README.md")
            _support.git(repo.path, "commit", "-m", "T-1")
            complete_run(repo, first["contract"]["run_id"])
            completed = service.record_campaign_task(
                {
                    "workspace": str(repo.path),
                    "campaign_id": campaign_id,
                    "task_id": "T-1",
                    "status": "complete",
                    "run_workspace": str(repo.path),
                    "run_id": first["contract"]["run_id"],
                }
            )
            self.assertEqual(
                _support.git(repo.path, "rev-parse", "HEAD"),
                completed["task"]["run"]["head_sha"],
            )

            service.record_campaign_task(
                {
                    "workspace": str(repo.path),
                    "campaign_id": campaign_id,
                    "task_id": "T-2",
                    "status": "in_progress",
                }
            )
            second = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Implement T-2",
                    "done_when": ["T-2 works"],
                    "campaign": {**parent, "task_id": "T-2"},
                }
            )
            self.assertEqual(
                completed["task"]["run"]["head_sha"],
                second["contract"]["base_sha"],
            )
            self.assertEqual("high", second["contract"]["risk"])

    def test_runtime_upgrade_is_allowed_only_between_task_waves(self) -> None:
        with _support.TempRepo() as repo, patch(
            "agent_harness.service._runtime_version",
            return_value="0.2.1+codex.20260825000000",
        ):
            service = HarnessService({})
            campaign = service.create_campaign(
                campaign_arguments(
                    repo,
                    tasks=[
                        task("T-1", kind="implementation"),
                        task("T-2", kind="implementation"),
                    ],
                    with_spec=True,
                )
            )
            campaign_id = campaign["contract"]["campaign_id"]
            common = {"workspace": str(repo.path), "campaign_id": campaign_id}
            service.record_campaign_task(
                {**common, "task_id": "T-1", "status": "in_progress"}
            )
            with patch(
                "agent_harness.service._runtime_version",
                return_value="0.2.1+codex.20260825010000",
            ):
                run = service.create_run(
                    {
                        "workspace": str(repo.path),
                        "goal": "Implement T-1",
                        "done_when": ["T-1 works"],
                        "campaign": {
                            **common,
                            "task_id": "T-1",
                        },
                    }
                )
            self.assertEqual(
                "0.2.1+codex.20260825010000",
                run["contract"]["runtime_version"],
            )
            service.record_campaign_task(
                {**common, "task_id": "T-2", "status": "in_progress"}
            )
            with patch(
                "agent_harness.service._runtime_version",
                return_value="0.2.1+codex.20260825020000",
            ), self.assertRaisesRegex(StateError, "between task waves"):
                service.create_run(
                    {
                        "workspace": str(repo.path),
                        "goal": "Implement T-2",
                        "done_when": ["T-2 works"],
                        "campaign": {**common, "task_id": "T-2"},
                    }
                )

    def test_campaign_runtime_rejects_a_downgrade(self) -> None:
        with _support.TempRepo() as repo, patch(
            "agent_harness.service._runtime_version",
            return_value="0.2.1+codex.20260825020000",
        ):
            service = HarnessService({})
            campaign = service.create_campaign(
                campaign_arguments(
                    repo,
                    tasks=[task("T-1", kind="implementation")],
                )
            )
            common = {
                "workspace": str(repo.path),
                "campaign_id": campaign["contract"]["campaign_id"],
                "task_id": "T-1",
            }
            service.record_campaign_task({**common, "status": "in_progress"})
            with patch(
                "agent_harness.service._runtime_version",
                return_value="0.2.1+codex.20260825010000",
            ), self.assertRaisesRegex(StateError, "downgraded"):
                service.create_run(
                    {
                        "workspace": str(repo.path),
                        "goal": "Implement T-1",
                        "done_when": ["T-1 works"],
                        "campaign": common,
                    }
                )

    def test_implementation_task_requires_completed_v1_run(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            campaign = service.create_campaign(
                campaign_arguments(repo, tasks=[task("T-1", kind="implementation")])
            )
            campaign_id = campaign["contract"]["campaign_id"]
            run = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Implement T-1",
                    "done_when": ["T-1 works"],
                }
            )
            run_id = run["contract"]["run_id"]
            common = {
                "workspace": str(repo.path),
                "campaign_id": campaign_id,
                "task_id": "T-1",
                "status": "complete",
                "run_workspace": str(repo.path),
                "run_id": run_id,
            }
            with self.assertRaises(StateError):
                service.record_campaign_task(common)

            complete_run(repo, run_id)
            completed = service.record_campaign_task(common)
            self.assertEqual(run_id, completed["task"]["run"]["run_id"])

    def test_implementation_run_must_match_task_workspace_and_base(self) -> None:
        with _support.TempRepo() as repo, _support.TempRepo() as other:
            service = HarnessService({})
            mismatched_base = "a" * 40
            campaign = service.create_campaign(
                campaign_arguments(
                    repo,
                    tasks=[
                        {
                            **task("T-1", kind="implementation"),
                            "workspace": str(repo.path),
                            "base_sha": mismatched_base,
                        }
                    ],
                )
            )
            campaign_id = campaign["contract"]["campaign_id"]
            local_run = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Implement T-1",
                    "done_when": ["T-1 works"],
                }
            )
            complete_run(repo, local_run["contract"]["run_id"])
            with self.assertRaisesRegex(StateError, "base_sha"):
                service.record_campaign_task(
                    {
                        "workspace": str(repo.path),
                        "campaign_id": campaign_id,
                        "task_id": "T-1",
                        "status": "complete",
                        "run_workspace": str(repo.path),
                        "run_id": local_run["contract"]["run_id"],
                    }
                )

            matching_campaign = service.create_campaign(
                campaign_arguments(
                    repo,
                    tasks=[task("T-2", kind="implementation")],
                )
            )
            foreign_run = service.create_run(
                {
                    "workspace": str(other.path),
                    "goal": "Implement T-2",
                    "done_when": ["T-2 works"],
                }
            )
            complete_run(other, foreign_run["contract"]["run_id"])
            with self.assertRaisesRegex(StateError, "workspace"):
                service.record_campaign_task(
                    {
                        "workspace": str(repo.path),
                        "campaign_id": matching_campaign["contract"]["campaign_id"],
                        "task_id": "T-2",
                        "status": "complete",
                        "run_workspace": str(other.path),
                        "run_id": foreign_run["contract"]["run_id"],
                    }
                )

    def test_completed_run_cannot_be_reused_by_another_task(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            campaign = service.create_campaign(
                campaign_arguments(
                    repo,
                    tasks=[
                        task("T-1", kind="implementation"),
                        task("T-2", kind="implementation"),
                    ],
                    with_spec=True,
                )
            )
            run = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Implement both tasks",
                    "done_when": ["Both tasks work"],
                }
            )
            run_id = run["contract"]["run_id"]
            complete_run(repo, run_id)
            common = {
                "workspace": str(repo.path),
                "campaign_id": campaign["contract"]["campaign_id"],
                "status": "complete",
                "run_workspace": str(repo.path),
                "run_id": run_id,
            }
            service.record_campaign_task({**common, "task_id": "T-1"})
            with self.assertRaisesRegex(StateError, "already linked"):
                service.record_campaign_task({**common, "task_id": "T-2"})

    def test_multi_task_repository_requires_combined_integration_run(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            campaign = service.create_campaign(
                campaign_arguments(
                    repo,
                    tasks=[
                        task("T-1", kind="implementation"),
                        task("T-2", kind="implementation"),
                    ],
                    with_spec=True,
                )
            )
            campaign_id = campaign["contract"]["campaign_id"]
            common = {"workspace": str(repo.path), "campaign_id": campaign_id}
            for task_id in ("T-1", "T-2"):
                run = service.create_run(
                    {
                        "workspace": str(repo.path),
                        "goal": f"Implement {task_id}",
                        "done_when": [f"{task_id} works"],
                    }
                )
                complete_run(repo, run["contract"]["run_id"])
                service.record_campaign_task(
                    {
                        **common,
                        "task_id": task_id,
                        "status": "complete",
                        "run_workspace": str(repo.path),
                        "run_id": run["contract"]["run_id"],
                    }
                )
            with self.assertRaisesRegex(StateError, "combined integration run"):
                service.seal_campaign_candidate(
                    {**common, "summary": "candidate"}
                )

    def test_combined_integration_run_covers_multi_task_repository(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as parent:
            _support.prepare_openspec_change(repo, commit=True)
            workspaces = {
                task_id: Path(parent) / task_id
                for task_id in ("T-1", "T-2", "integrate")
            }
            for workspace in workspaces.values():
                _support.git(
                    repo.path,
                    "worktree",
                    "add",
                    "--detach",
                    str(workspace),
                    "HEAD",
                )
            service = HarnessService({})
            arguments = campaign_arguments(
                repo,
                tasks=[
                    {
                        **task("T-1", kind="implementation"),
                        "workspace": str(workspaces["T-1"]),
                    },
                    {
                        **task("T-2", kind="implementation"),
                        "workspace": str(workspaces["T-2"]),
                    },
                    {
                        **task(
                            "integrate",
                            kind="implementation",
                            dependencies=["T-1", "T-2"],
                        ),
                        "role": "integration",
                        "workspace": str(workspaces["integrate"]),
                    },
                ],
            )
            arguments["spec"] = {
                "kind": "openspec",
                "change_id": "add-feature",
            }
            campaign = service.create_campaign(arguments)
            common = {
                "workspace": str(repo.path),
                "campaign_id": campaign["contract"]["campaign_id"],
            }
            changes = {
                "T-1": {"one.txt": "one\n"},
                "T-2": {"two.txt": "two\n"},
                "integrate": {"one.txt": "one\n", "two.txt": "two\n"},
            }
            for task_id in ("T-1", "T-2", "integrate"):
                workspace = workspaces[task_id]
                run = service.create_run(
                    {
                        "workspace": str(workspace),
                        "goal": f"Complete {task_id}",
                        "done_when": [f"{task_id} works"],
                    }
                )
                for name, content in changes[task_id].items():
                    (workspace / name).write_text(content, encoding="utf-8")
                complete_run(workspace, run["contract"]["run_id"])
                service.record_campaign_task(
                    {
                        **common,
                        "task_id": task_id,
                        "status": "complete",
                        "run_workspace": str(workspace),
                        "run_id": run["contract"]["run_id"],
                    }
                )
            sealed = service.seal_campaign_candidate(
                {**common, "summary": "candidate"}
            )
            self.assertEqual("ready", sealed["candidate"]["readiness"])

    def test_terminal_task_attempt_can_restart_without_losing_history(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            campaign = service.create_campaign(campaign_arguments(repo))
            common = {
                "workspace": str(repo.path),
                "campaign_id": campaign["contract"]["campaign_id"],
                "task_id": "T-1",
            }
            for status in ("failed", "interrupted", "blocked"):
                service.record_campaign_task({**common, "status": status})
                restarted = service.record_campaign_task(
                    {**common, "status": "in_progress"}
                )
                self.assertEqual("in_progress", restarted["task"]["status"])

    def test_replay_cannot_read_history_before_candidate_is_sealed(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            campaign = service.create_campaign(
                campaign_arguments(repo, mode="replay")
            )
            campaign_id = campaign["contract"]["campaign_id"]
            common = {"workspace": str(repo.path), "campaign_id": campaign_id}
            rubric = {
                "scope": 4,
                "behavior": 3,
                "architecture": 3,
                "tests": 4,
                "operability": 2,
            }
            comparison = {
                "rubric": rubric,
                "cutoff_rubric": {name: 4 for name in rubric},
                "candidate_readiness": "partial",
            }
            with self.assertRaises(StateError):
                service.record_campaign_comparison({**common, **comparison})
            service.record_campaign_task(
                {**common, "task_id": "T-1", "status": "complete"}
            )
            service.seal_campaign_candidate({**common, "summary": "candidate"})
            compared = service.record_campaign_comparison(
                {
                    **common,
                    **comparison,
                    "historical_refs": ["DEMO-1", "PR-2"],
                }
            )
            self.assertEqual(80, compared["comparison"]["overall_percent"])
            self.assertEqual(
                100, compared["comparison"]["cutoff_fidelity_percent"]
            )
            self.assertEqual(
                80, compared["comparison"]["historical_similarity_percent"]
            )
            finished = service.finish_campaign({**common, "status": "complete"})
            self.assertFalse(finished["terminal"]["external_actions_authorized"])
            self.assertEqual("evaluated", finished["terminal"]["campaign_status"])
            self.assertEqual(
                "partial", finished["terminal"]["candidate_readiness"]
            )
            repeated = service.finish_campaign({**common, "status": "complete"})
            self.assertTrue(repeated["deduplicated"])
            repeated_comparison = service.record_campaign_comparison(
                {
                    **common,
                    **comparison,
                    "historical_refs": ["DEMO-1", "PR-2"],
                }
            )
            self.assertTrue(repeated_comparison["deduplicated"])

    def test_blocking_intervention_must_be_resolved_before_seal(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            campaign = service.create_campaign(campaign_arguments(repo))
            campaign_id = campaign["contract"]["campaign_id"]
            common = {"workspace": str(repo.path), "campaign_id": campaign_id}
            service.record_campaign_task(
                {**common, "task_id": "T-1", "status": "complete"}
            )
            unresolved = {
                **common,
                "intervention_id": "I-1",
                "kind": "blocking_question",
                "blocking": True,
                "resolved": False,
                "reason": "Need a product decision",
            }
            service.record_campaign_intervention(unresolved)
            with self.assertRaises(StateError):
                service.seal_campaign_candidate({**common, "summary": "candidate"})
            resolved = service.record_campaign_intervention(
                {**unresolved, "resolved": True, "outcome": "Use option A"}
            )
            self.assertTrue(resolved["intervention"]["resolved"])
            sealed = service.seal_campaign_candidate(
                {**common, "summary": "candidate"}
            )
            self.assertEqual(
                1,
                sealed["candidate"]["human_interventions"][
                    "blocking_questions"
                ],
            )

    def test_intervention_text_is_sanitized(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            campaign = service.create_campaign(campaign_arguments(repo))
            campaign_id = campaign["contract"]["campaign_id"]
            recorded = service.record_campaign_intervention(
                {
                    "workspace": str(repo.path),
                    "campaign_id": campaign_id,
                    "intervention_id": "I-1",
                    "kind": "context",
                    "blocking": False,
                    "resolved": True,
                    "reason": "api_key=super-secret-value",
                    "outcome": "token=another-secret-value",
                }
            )
            serialized = str(recorded)
            self.assertNotIn("super-secret-value", serialized)
            self.assertNotIn("another-secret-value", serialized)
            self.assertIn("<redacted>", serialized)

    def test_campaign_contract_text_is_sanitized(self) -> None:
        with _support.TempRepo() as repo:
            arguments = campaign_arguments(repo)
            arguments["goal"] = "Use token=super-secret-value"
            arguments["tasks"][0]["goal"] = "Read api_key=another-secret-value"
            contract = HarnessService({}).create_campaign(arguments)["contract"]
            serialized = str(contract)
            self.assertNotIn("super-secret-value", serialized)
            self.assertNotIn("another-secret-value", serialized)
            self.assertIn("<redacted>", serialized)

    def test_candidate_rejects_paths_not_covered_by_v1_runs(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            campaign = service.create_campaign(campaign_arguments(repo))
            common = {
                "workspace": str(repo.path),
                "campaign_id": campaign["contract"]["campaign_id"],
            }
            service.record_campaign_task(
                {**common, "task_id": "T-1", "status": "complete"}
            )
            (repo.path / "README.md").write_text("uncovered\n", encoding="utf-8")
            with self.assertRaisesRegex(StateError, "not covered"):
                service.seal_campaign_candidate({**common, "summary": "candidate"})

            (repo.path / "README.md").write_text("initial\n", encoding="utf-8")
            sealed = service.seal_campaign_candidate(
                {**common, "summary": "candidate"}
            )
            self.assertEqual(
                [], sealed["candidate"]["git_snapshots"][0]["changed_paths"]
            )

    def test_dirty_workspace_cannot_start_campaign(self) -> None:
        with _support.TempRepo() as repo:
            (repo.path / "README.md").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(InputError, "isolated worktree"):
                HarnessService({}).create_campaign(campaign_arguments(repo))

    def test_replay_requires_timezone_cutoff(self) -> None:
        with _support.TempRepo() as repo:
            arguments = campaign_arguments(repo, mode="replay")
            arguments["cutoff_at"] = "2026-01-01T00:00:00"
            with self.assertRaises(InputError):
                HarnessService({}).create_campaign(arguments)

    def test_corrupt_campaign_state_is_rejected(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService({})
            campaign = service.create_campaign(campaign_arguments(repo))
            campaign_id = campaign["contract"]["campaign_id"]
            store = CampaignStore.for_workspace(repo.path)
            (store.campaign_dir(campaign_id) / "state.json").write_text(
                "{broken", encoding="utf-8"
            )
            with self.assertRaises(StateError):
                service.get_campaign(
                    {"workspace": str(repo.path), "campaign_id": campaign_id}
                )


if __name__ == "__main__":
    unittest.main()
