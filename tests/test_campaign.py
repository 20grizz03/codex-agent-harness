from __future__ import annotations

import stat
import unittest

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
) -> dict:
    arguments = {
        "workspace": str(repo.path),
        "title": "Epic campaign",
        "goal": "Deliver the epic",
        "done_when": ["All campaign tasks are complete"],
        "source": {"kind": "jira", "ref": "DEMO-1"},
        "mode": mode,
        "tasks": tasks or [task("T-1")],
    }
    if mode == "replay":
        arguments["cutoff_at"] = "2026-01-01T00:00:00Z"
    return arguments


def complete_run(repo: _support.TempRepo, run_id: str) -> None:
    run_store = RunStore.for_workspace(repo.path)
    run_contract = run_store.read_contract(run_id)
    run_state = run_store.read_state(run_id)
    fingerprint, changed_paths = diff_fingerprint(
        resolve_repo(repo.path),
        base_sha=run_contract["base_sha"],
    )
    run_state["diff_fingerprint"] = fingerprint
    run_state["changed_paths"] = changed_paths
    run_state["phase"] = "complete"
    run_state["terminal"] = {"status": "complete"}
    run_store.save_state(run_id, run_state)


class CampaignTests(unittest.TestCase):
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
            with self.assertRaises(StateError):
                service.record_campaign_comparison({**common, "rubric": rubric})
            service.record_campaign_task(
                {**common, "task_id": "T-1", "status": "complete"}
            )
            service.seal_campaign_candidate({**common, "summary": "candidate"})
            compared = service.record_campaign_comparison(
                {
                    **common,
                    "rubric": rubric,
                    "historical_refs": ["DEMO-1", "PR-2"],
                }
            )
            self.assertEqual(80, compared["comparison"]["overall_percent"])
            finished = service.finish_campaign({**common, "status": "complete"})
            self.assertFalse(finished["terminal"]["external_actions_authorized"])
            repeated = service.finish_campaign({**common, "status": "complete"})
            self.assertTrue(repeated["deduplicated"])
            repeated_comparison = service.record_campaign_comparison(
                {
                    **common,
                    "rubric": rubric,
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
