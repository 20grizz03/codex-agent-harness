from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import _support

from agent_harness.service import HarnessService
from agent_harness.campaign import CampaignStore
from agent_harness.review import build_stage_prompt
from agent_harness.store import RunStore
from agent_harness.util import StateError


def run_planned_checks(
    service: HarnessService, repo: Path, run_id: str
) -> dict:
    plan = service.plan_checks({"workspace": str(repo), "run_id": run_id})
    for check in plan["checks"]:
        completed = subprocess.run(
            check["argv"],
            cwd=str(repo),
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
    return plan


def wait_for_stage(
    service: HarnessService, repo: Path, run_id: str, stage_id: str
) -> dict:
    return _support.wait_until(
        lambda: (
            result["terminal"]
            if (
                result := service.poll_stage(
                    {
                        "workspace": str(repo),
                        "run_id": run_id,
                        "stage_id": stage_id,
                        "wait_seconds": 0.1,
                    }
                )
            ).get("terminal")
            else None
        )
    )


class CodexWriterWorkflowTests(unittest.TestCase):
    def test_late_stage_completion_preserves_explicit_run_terminal(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory))
            run_id = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                }
            )["contract"]["run_id"]
            store = RunStore.for_workspace(repo.path)
            stage_id = f"{run_id}:critic:1"
            state = store.read_state(run_id)
            state["phase"] = "interrupted"
            state["terminal"] = {
                "status": "interrupted",
                "summary": "User stopped the run",
            }
            state["stages"] = {
                stage_id: {
                    "profile": "critic",
                    "lifecycle_state": "running",
                    "error": "stale recovery error",
                }
            }
            store.save_state(run_id, state)

            service._on_stage_terminal(
                str(repo.path),
                run_id,
                stage_id,
                {
                    "lifecycle_state": "completed",
                    "result": _support.PASS_REVIEW,
                    "telemetry": {},
                },
            )

            persisted = store.read_state(run_id)
            self.assertEqual("interrupted", persisted["phase"])
            self.assertEqual("User stopped the run", persisted["terminal"]["summary"])
            self.assertEqual(
                "completed", persisted["stages"][stage_id]["lifecycle_state"]
            )
            self.assertNotIn("error", persisted["stages"][stage_id])
            self.assertIsNone(store.read_review(run_id)["review"])

    def test_finish_run_rejects_an_active_model_stage(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory), FAKE_CLAUDE_MODE="hang")
            run_id = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                }
            )["contract"]["run_id"]
            (repo.path / "README.md").write_text("changed\n", encoding="utf-8")
            run_planned_checks(service, repo.path, run_id)
            started = service.start_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "profile": "critic",
                }
            )

            with self.assertRaisesRegex(StateError, "cancel_stage"):
                service.finish_run(
                    {
                        "workspace": str(repo.path),
                        "run_id": run_id,
                        "status": "interrupted",
                    }
                )
            service.cancel_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "stage_id": started["stage_id"],
                }
            )

    def test_high_risk_review_focus_uses_escalated_state_risk(self) -> None:
        prompt = build_stage_prompt(
            profile="critic",
            contract={"risk": "medium"},
            state={"risk": "high", "check_results": {}},
        )
        self.assertIn("security boundaries, authentication", prompt)

    def _service(
        self, directory: Path, result: dict | None = None, **updates: str
    ) -> HarnessService:
        fake = _support.make_fake_claude(directory)
        return HarnessService(
            _support.fake_environment(fake, result or _support.PASS_REVIEW, **updates)
        )

    def test_clean_pass_flow_completes_and_is_idempotent(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory))
            run = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update the README",
                    "done_when": ["README contains the new behavior"],
                    "risk": "high",
                }
            )
            run_id = run["contract"]["run_id"]
            (repo.path / "README.md").write_text("new behavior\n", encoding="utf-8")
            plan = run_planned_checks(service, repo.path, run_id)
            self.assertEqual(["git-diff-check"], [c["name"] for c in plan["checks"]])

            started = service.start_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "profile": "critic",
                }
            )
            terminal = wait_for_stage(
                service, repo.path, run_id, started["stage_id"]
            )
            self.assertEqual("completed", terminal["lifecycle_state"])
            _support.wait_until(
                lambda: service.get_run(
                    {"workspace": str(repo.path), "run_id": run_id}
                )["review"]["review"]
            )
            service.record_review_resolution(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "resolutions": [],
                }
            )
            finished = service.finish_run(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "status": "complete",
                    "summary": "All local gates passed",
                }
            )
            self.assertEqual("complete", finished["phase"])
            self.assertEqual("high", service.get_run(
                {"workspace": str(repo.path), "run_id": run_id}
            )["state"]["risk"])
            repeated = service.finish_run(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "status": "complete",
                }
            )
            self.assertTrue(repeated["deduplicated"])

    def test_confirmed_anthropic_limit_allows_one_codex_fallback_review(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            service = self._service(
                Path(directory), FAKE_CLAUDE_MODE="limit_result"
            )
            run_id = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                }
            )["contract"]["run_id"]
            (repo.path / "README.md").write_text("changed\n", encoding="utf-8")
            run_planned_checks(service, repo.path, run_id)
            started = service.start_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "profile": "critic",
                }
            )
            terminal = wait_for_stage(
                service, repo.path, run_id, started["stage_id"]
            )
            self.assertEqual("anthropic_limit", terminal["failure_kind"])
            state = _support.wait_until(
                lambda: (
                    current
                    if (
                        current := service.get_run(
                            {"workspace": str(repo.path), "run_id": run_id}
                        )["state"]
                    ).get("phase")
                    == "reviewing"
                    else None
                )
            )
            self.assertIsNone(state["terminal"])

            service.record_review_resolution(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "review": _support.PASS_REVIEW,
                    "resolutions": [],
                }
            )
            finished = service.finish_run(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "status": "complete",
                }
            )
            self.assertEqual("complete", finished["phase"])
            persisted = service.get_run(
                {"workspace": str(repo.path), "run_id": run_id}
            )
            self.assertEqual(
                "codex_fallback", persisted["review"]["review"]["origin"]
            )
            critic = next(iter(persisted["state"]["stages"].values()))
            self.assertEqual("anthropic_limit", critic["failure_kind"])

    def test_campaign_limit_skips_until_one_later_probe_recovers(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            _support.prepare_openspec_change(repo, commit=True)
            marker = Path(directory) / "invoked"
            service = self._service(
                Path(directory),
                FAKE_CLAUDE_MODE="limit_result",
                FAKE_CLAUDE_MARKER=str(marker),
                AGENT_HARNESS_ANTHROPIC_COOLDOWN_SECONDS="3600",
            )
            tasks = [
                {
                    "id": task_id,
                    "title": task_id,
                    "goal": f"Complete {task_id}",
                    "done_when": [f"{task_id} works"],
                    "kind": "implementation",
                }
                for task_id in ("T-1", "T-I", "T-2", "T-3")
            ]
            campaign = service.create_campaign(
                {
                    "workspace": str(repo.path),
                    "title": "Cooldown campaign",
                    "goal": "Verify provider recovery",
                    "done_when": ["All tasks are reviewed"],
                    "source": {"kind": "local", "ref": "cooldown-test"},
                    "risk": "medium",
                    "spec": {
                        "kind": "openspec",
                        "change_id": "add-feature",
                        "storage": "repository",
                    },
                    "tasks": tasks,
                }
            )
            campaign_id = campaign["contract"]["campaign_id"]
            campaign_common = {
                "workspace": str(repo.path),
                "campaign_id": campaign_id,
            }

            def create_checked_run(task_id: str, *, allow_dirty: bool) -> str:
                service.record_campaign_task(
                    {
                        **campaign_common,
                        "task_id": task_id,
                        "status": "in_progress",
                    }
                )
                run = service.create_run(
                    {
                        "workspace": str(repo.path),
                        "goal": f"Implement {task_id}",
                        "done_when": [f"{task_id} works"],
                        "allow_dirty": allow_dirty,
                        "campaign": {
                            **campaign_common,
                            "task_id": task_id,
                        },
                    }
                )
                run_id = run["contract"]["run_id"]
                run_planned_checks(service, repo.path, run_id)
                return run_id

            (repo.path / "README.md").write_text("changed\n", encoding="utf-8")
            first_run = create_checked_run("T-1", allow_dirty=True)
            first_stage = service.start_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": first_run,
                    "profile": "critic",
                }
            )
            first_terminal = wait_for_stage(
                service, repo.path, first_run, first_stage["stage_id"]
            )
            self.assertEqual("anthropic_limit", first_terminal["failure_kind"])
            store = CampaignStore.for_workspace(repo.path)
            _support.wait_until(
                lambda: store.read_state(campaign_id)["provider_circuits"][
                    "anthropic"
                ]["status"]
                == "open"
            )
            service.record_review_resolution(
                {
                    "workspace": str(repo.path),
                    "run_id": first_run,
                    "review": _support.PASS_REVIEW,
                    "resolutions": [],
                }
            )
            service.finish_run(
                {
                    "workspace": str(repo.path),
                    "run_id": first_run,
                    "status": "complete",
                }
            )
            service.record_campaign_task(
                {
                    **campaign_common,
                    "task_id": "T-1",
                    "status": "complete",
                    "run_workspace": str(repo.path),
                    "run_id": first_run,
                }
            )

            marker.unlink()
            service.record_campaign_task(
                {
                    **campaign_common,
                    "task_id": "T-I",
                    "status": "in_progress",
                }
            )
            implement_run = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Implement T-I with Claude",
                    "done_when": ["T-I works"],
                    "allow_dirty": True,
                    "writer": "claude",
                    "writer_explicit": True,
                    "campaign": {**campaign_common, "task_id": "T-I"},
                }
            )["contract"]["run_id"]
            skipped_implement = service.start_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": implement_run,
                    "profile": "implement",
                }
            )
            self.assertFalse(skipped_implement["claude_invoked"])
            self.assertEqual(
                "failed",
                service.get_run(
                    {"workspace": str(repo.path), "run_id": implement_run}
                )["state"]["phase"],
            )
            self.assertEqual(
                "open",
                store.read_state(campaign_id)["provider_circuits"]["anthropic"][
                    "status"
                ],
            )
            self.assertFalse(marker.exists())

            second_run = create_checked_run("T-2", allow_dirty=True)
            skipped = service.start_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": second_run,
                    "profile": "critic",
                }
            )
            self.assertFalse(skipped["claude_invoked"])
            self.assertFalse(marker.exists())
            service.record_review_resolution(
                {
                    "workspace": str(repo.path),
                    "run_id": second_run,
                    "review": _support.PASS_REVIEW,
                    "resolutions": [],
                }
            )
            service.finish_run(
                {
                    "workspace": str(repo.path),
                    "run_id": second_run,
                    "status": "complete",
                }
            )
            service.record_campaign_task(
                {
                    **campaign_common,
                    "task_id": "T-2",
                    "status": "complete",
                    "run_workspace": str(repo.path),
                    "run_id": second_run,
                }
            )

            state = store.read_state(campaign_id)
            state["provider_circuits"]["anthropic"]["cooldown_until"] = (
                "2000-01-01T00:00:00Z"
            )
            store.save_state(campaign_id, state)
            service.environ["FAKE_CLAUDE_MODE"] = "success"
            third_run = create_checked_run("T-3", allow_dirty=True)
            probe = service.start_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": third_run,
                    "profile": "critic",
                }
            )
            self.assertTrue(probe["claude_invoked"])
            terminal = wait_for_stage(
                service, repo.path, third_run, probe["stage_id"]
            )
            self.assertEqual("completed", terminal["lifecycle_state"])
            _support.wait_until(
                lambda: store.read_state(campaign_id)["provider_circuits"][
                    "anthropic"
                ]["status"]
                == "closed"
            )

    def test_generic_critic_failure_does_not_enable_fallback(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory), FAKE_CLAUDE_MODE="fail")
            run_id = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                }
            )["contract"]["run_id"]
            (repo.path / "README.md").write_text("changed\n", encoding="utf-8")
            run_planned_checks(service, repo.path, run_id)
            started = service.start_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "profile": "critic",
                }
            )
            wait_for_stage(service, repo.path, run_id, started["stage_id"])
            _support.wait_until(
                lambda: service.get_run(
                    {"workspace": str(repo.path), "run_id": run_id}
                )["state"]["phase"]
                == "failed"
            )
            with self.assertRaises(StateError):
                service.record_review_resolution(
                    {
                        "workspace": str(repo.path),
                        "run_id": run_id,
                        "review": _support.PASS_REVIEW,
                        "resolutions": [],
                    }
                )

    def test_failed_gate_prevents_review(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory))
            run_id = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                }
            )["contract"]["run_id"]
            (repo.path / "README.md").write_text("trailing whitespace   \n", encoding="utf-8")
            service.plan_checks({"workspace": str(repo.path), "run_id": run_id})
            service.record_check(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "check_name": "git-diff-check",
                    "exit_code": 2,
                    "duration_ms": 1,
                    "summary": "whitespace error",
                }
            )
            with self.assertRaises(StateError):
                service.start_stage(
                    {
                        "workspace": str(repo.path),
                        "run_id": run_id,
                        "profile": "critic",
                    }
                )

    def test_edit_invalidates_green_check_evidence(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory))
            run_id = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                }
            )["contract"]["run_id"]
            (repo.path / "README.md").write_text("first\n", encoding="utf-8")
            run_planned_checks(service, repo.path, run_id)
            (repo.path / "README.md").write_text("second\n", encoding="utf-8")
            with self.assertRaises(StateError):
                service.start_stage(
                    {
                        "workspace": str(repo.path),
                        "run_id": run_id,
                        "profile": "critic",
                    }
                )
            replanned = service.plan_checks(
                {"workspace": str(repo.path), "run_id": run_id}
            )
            self.assertFalse(replanned["deduplicated"])
            current = service.get_run(
                {"workspace": str(repo.path), "run_id": run_id}
            )["state"]
            self.assertEqual({}, current["check_results"][replanned["diff_fingerprint"]])

    def test_one_correction_reruns_checks_without_second_critic(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory), _support.finding_review())
            run_id = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs correctly",
                    "done_when": ["README contains correct behavior"],
                }
            )["contract"]["run_id"]
            (repo.path / "README.md").write_text("wrong behavior\n", encoding="utf-8")
            first_plan = run_planned_checks(service, repo.path, run_id)
            started = service.start_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "profile": "critic",
                }
            )
            wait_for_stage(service, repo.path, run_id, started["stage_id"])
            _support.wait_until(
                lambda: service.get_run(
                    {"workspace": str(repo.path), "run_id": run_id}
                )["review"]["review"]
            )

            first_resolution = service.record_review_resolution(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "resolutions": [
                        {
                            "finding_id": "F-1",
                            "disposition": "accepted",
                            "resolved": False,
                            "evidence": "README.md contradicts the contract",
                        }
                    ],
                }
            )
            self.assertEqual("correcting", first_resolution["phase"])
            (repo.path / "README.md").write_text("correct behavior\n", encoding="utf-8")
            second_plan = run_planned_checks(service, repo.path, run_id)
            self.assertNotEqual(
                first_plan["diff_fingerprint"], second_plan["diff_fingerprint"]
            )
            final_resolution = service.record_review_resolution(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "resolutions": [
                        {
                            "finding_id": "F-1",
                            "disposition": "accepted",
                            "resolved": True,
                            "evidence": "README.md now matches the contract",
                        }
                    ],
                }
            )
            self.assertEqual("reviewing", final_resolution["phase"])
            state = service.get_run(
                {"workspace": str(repo.path), "run_id": run_id}
            )["state"]
            self.assertEqual(1, state["correction_passes"])
            self.assertEqual(
                1,
                len(
                    [
                        stage
                        for stage in state["stages"].values()
                        if stage["profile"] == "critic"
                    ]
                ),
            )
            finished = service.finish_run(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "status": "complete",
                }
            )
            self.assertEqual("complete", finished["phase"])

    def test_unresolved_p1_with_no_correction_budget_needs_human(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory), _support.finding_review("P1"))
            run_id = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                    "max_correction_passes": 0,
                }
            )["contract"]["run_id"]
            (repo.path / "README.md").write_text("wrong\n", encoding="utf-8")
            run_planned_checks(service, repo.path, run_id)
            stage = service.start_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "profile": "critic",
                }
            )
            wait_for_stage(service, repo.path, run_id, stage["stage_id"])
            _support.wait_until(
                lambda: service.get_run(
                    {"workspace": str(repo.path), "run_id": run_id}
                )["review"]["review"]
            )
            result = service.record_review_resolution(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "resolutions": [
                        {
                            "finding_id": "F-1",
                            "disposition": "unverified",
                            "resolved": False,
                            "evidence": "Needs product confirmation",
                        }
                    ],
                }
            )
            self.assertEqual("needs_human", result["phase"])

    def test_blocking_question_becomes_needs_human(self) -> None:
        review = {
            "verdict": "blocked",
            "findings": [],
            "residual_risks": [],
            "blocking_question": "Which compatibility contract is authoritative?",
        }
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory), review)
            run_id = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                }
            )["contract"]["run_id"]
            (repo.path / "README.md").write_text("changed\n", encoding="utf-8")
            run_planned_checks(service, repo.path, run_id)
            stage = service.start_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "profile": "critic",
                }
            )
            wait_for_stage(service, repo.path, run_id, stage["stage_id"])
            _support.wait_until(
                lambda: service.get_run(
                    {"workspace": str(repo.path), "run_id": run_id}
                )["review"]["review"]
            )
            result = service.record_review_resolution(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "resolutions": [],
                }
            )
            self.assertEqual("needs_human", result["phase"])


class ClaudeWriterWorkflowTests(unittest.TestCase):
    def test_claude_writer_requires_explicit_authority(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService()
            with self.assertRaises(Exception):
                service.create_run(
                    {
                        "workspace": str(repo.path),
                        "goal": "Update docs",
                        "done_when": ["Docs are current"],
                        "writer": "claude",
                    }
                )

    def test_claude_writer_gets_independent_codex_review(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            fake = _support.make_fake_claude(Path(directory))
            service = HarnessService(
                _support.fake_environment(
                    fake,
                    _support.IMPLEMENT_RESULT,
                    FAKE_CLAUDE_WRITE_PATH=str(repo.path / "README.md"),
                    FAKE_CLAUDE_WRITE_CONTENT="implemented by claude\n",
                )
            )
            run_id = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                    "writer": "claude",
                    "writer_explicit": True,
                }
            )["contract"]["run_id"]
            stage = service.start_stage(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "profile": "implement",
                }
            )
            terminal = wait_for_stage(service, repo.path, run_id, stage["stage_id"])
            self.assertEqual("completed", terminal["lifecycle_state"])
            _support.wait_until(
                lambda: service.get_run(
                    {"workspace": str(repo.path), "run_id": run_id}
                )["state"]["phase"]
                == "writing"
            )
            run_planned_checks(service, repo.path, run_id)
            service.record_review_resolution(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "review": _support.PASS_REVIEW,
                    "resolutions": [],
                }
            )
            finished = service.finish_run(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "status": "complete",
                }
            )
            self.assertEqual("complete", finished["phase"])
            review = service.get_run(
                {"workspace": str(repo.path), "run_id": run_id}
            )["review"]["review"]
            self.assertEqual("codex", review["origin"])


if __name__ == "__main__":
    unittest.main()
