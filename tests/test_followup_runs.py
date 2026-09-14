from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support
from agent_harness.followup import FollowupService
from agent_harness.followup_git import verified_baseline
from agent_harness.git_repo import resolve_repo
from agent_harness.mcp_server import McpServer
from agent_harness.service import HarnessService
from agent_harness.util import InputError, StateError
from test_workflow import run_planned_checks, wait_for_stage


class FollowupRunTests(unittest.TestCase):
    def setUp(self):
        self.repo = _support.TempRepo()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.baseline, self.candidate = root / "baseline", root / "candidate"
        git = _support.git
        self.base = git(self.repo.path, "rev-parse", "HEAD")
        git(self.repo.path, "worktree", "add", "--detach", str(self.baseline), self.base)
        (self.baseline / "feature.txt").write_text("initial feature\n")
        git(self.baseline, "add", ".")
        git(self.baseline, "commit", "-m", "feature")
        self.old_head = git(self.baseline, "rev-parse", "HEAD")
        (self.repo.path / "parent.txt").write_text("accepted upstream\n")
        git(self.repo.path, "add", ".")
        git(self.repo.path, "commit", "-m", "parent")
        self.parent = git(self.repo.path, "rev-parse", "HEAD")
        git(self.repo.path, "worktree", "add", "--detach", str(self.candidate), self.old_head)
        git(self.candidate, "merge", "--no-edit", self.parent)
        fake = _support.make_fake_claude(root)
        self.service = HarnessService(_support.fake_environment(fake))
        self.server = McpServer(self.service)
        self.journal = FollowupService(str(self.repo.path))
        self.document = self.service.create_followup({
            "workspace": str(self.repo.path), "followup_id": "chain", "owner_id": "lead",
            "source": {"workspace": str(self.repo.path), "base_sha": self.base, "head_sha": self.parent},
            "tasks": [{"id": "child", "workspace": str(self.baseline), "base_sha": self.base,
                       "head_sha": self.old_head, "active": False, "published": True,
                       "build_checks": ["build"], "test_checks": ["affected"],
                       "check_bindings": {"build": "1" * 64, "affected": "2" * 64}}],
        })
        self.counter = 0
        self.action("begin", {"task_id": "child", "workspace": str(self.candidate), "mode": "semantic"})
        self.reference = self.journal._reference(self.document["contract"], self.document["state"], "child")

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def action(self, action, data):
        self.counter += 1
        self.document = self.service.record_followup({
            "workspace": str(self.repo.path), "followup_id": "chain", "owner_id": "lead",
            "expected_revision": self.document["state"]["revision"], "request_id": f"request-{self.counter}",
            "action": action, "data": data,
        })
        return self.document

    def create_run(self, **extra):
        return self.service.create_run({"workspace": str(self.candidate), "followup_ref": self.reference,
                                       "goal": "Preserve feature under updated parent",
                                       "done_when": ["Full payload and delivery semantics are preserved"], **extra})

    def test_semantic_followup_full_fake_review_cycle_and_historical_immutability(self):
        self._complete_cycle(commit_after_review=False)

    def test_exact_commit_after_review_keeps_provenance_without_new_model_call(self):
        self._complete_cycle(commit_after_review=True)

    def test_reopen_drops_complete_run_authority_and_advances_attempt(self):
        self._complete_cycle(commit_after_review=False)
        old_run = self.document["state"]["tasks"]["child"]["run_id"]
        old_workspace = self.candidate
        self.action("reopen", {"task_id": "child", "summary": "correct reviewed behavior"})
        self.assertNotIn("run_id", self.document["state"]["tasks"]["child"])
        next_workspace = self.candidate.parent / "next-candidate"
        _support.git(self.repo.path, "worktree", "add", "--detach", str(next_workspace), _support.git(self.candidate, "rev-parse", "HEAD"))
        self.action("begin", {"task_id": "child", "workspace": str(next_workspace), "mode": "semantic"})
        self.candidate = next_workspace
        with self.assertRaises(StateError):
            self.create_run()
        self.reference = self.journal._reference(self.document["contract"], self.document["state"], "child")
        self.assertEqual(2, self.create_run()["contract"]["followup_ref"]["attempt"])
        self.assertEqual("complete", self.service.get_run({"workspace": str(old_workspace), "run_id": old_run})["state"]["phase"])

    def _complete_cycle(self, *, commit_after_review):
        created = self.create_run()
        contract = created["contract"]
        self.assertEqual(self.parent, contract["base_sha"])
        self.assertEqual(self.reference, contract["followup_ref"])
        run_id = contract["run_id"]
        if commit_after_review:
            (self.candidate / "feature.txt").write_text("corrected payload contract\n")
        run_planned_checks(self.service, self.candidate, run_id)
        stage = self.service.start_stage({"workspace": str(self.candidate), "run_id": run_id, "profile": "critic"})
        wait_for_stage(self.service, self.candidate, run_id, stage["stage_id"])
        finished = self.service.finish_run({"workspace": str(self.candidate), "run_id": run_id, "status": "complete"})
        self.assertEqual("complete", finished["phase"])
        if commit_after_review:
            _support.git(self.candidate, "add", "feature.txt")
            _support.git(self.candidate, "commit", "-m", "save reviewed correction")
        self.action("candidate", {"task_id": "child"})
        fingerprint = self.document["state"]["tasks"]["child"]["candidate"]["diff_fingerprint"]
        for name in ("build", "affected"):
            self.action("check", {"task_id": "child", "name": name, "diff_fingerprint": fingerprint,
                                  "check_fingerprint": self.document["state"]["tasks"]["child"]["check_bindings"][name],
                                  "exit_code": 0, "duration_ms": 1})
        self.action("finish", {"task_id": "child", "run_id": run_id})
        self.assertTrue(self.service.get_followup({"workspace": str(self.repo.path), "followup_id": "chain"})["readiness"]["child"]["ready"])
        self.assertEqual(contract, self.service.get_run({"workspace": str(self.candidate), "run_id": run_id})["contract"])
        if commit_after_review:
            context = resolve_repo(self.candidate)
            proof = verified_baseline(context, self.parent, context.head_sha, run_id)
            self.assertEqual(run_id, proof["run_id"])
            (self.candidate / "feature.txt").write_text("unreviewed behavior\n")
            _support.git(self.candidate, "add", "feature.txt")
            _support.git(self.candidate, "commit", "-m", "unreviewed change")
            context = resolve_repo(self.candidate)
            with self.assertRaises(StateError):
                verified_baseline(context, self.parent, context.head_sha, run_id)
        else:
            self.assertTrue(self.service.finish_run({"workspace": str(self.candidate), "run_id": run_id, "status": "complete"})["deduplicated"])

    def test_new_attempt_invalidates_old_run_without_overwriting_it(self):
        created = self.create_run()
        run_id = created["contract"]["run_id"]
        self.action("pause", {"task_id": "child", "status": "interrupted"})
        self.action("begin", {"task_id": "child", "workspace": str(self.candidate), "mode": "semantic"})
        with self.assertRaises(StateError):
            self.service.plan_checks({"workspace": str(self.candidate), "run_id": run_id})
        self.assertEqual(created["contract"], self.service.get_run({"workspace": str(self.candidate), "run_id": run_id})["contract"])
        with self.assertRaises(StateError):
            self.create_run()

    def test_abandon_rejects_live_run_authority_but_keeps_run_history(self):
        created = self.create_run()
        run_id = created["contract"]["run_id"]
        self.action("pause", {"task_id": "child", "status": "interrupted"})
        self.action("abandon", {"summary": "cancel this correction"})
        with self.assertRaisesRegex(StateError, "cancelled"):
            self.service.plan_checks({"workspace": str(self.candidate), "run_id": run_id})
        self.assertEqual(created["contract"], self.service.get_run({"workspace": str(self.candidate), "run_id": run_id})["contract"])

    def test_reference_rejects_base_override_campaign_and_boolean_revision(self):
        with self.assertRaises(InputError):
            self.create_run(base_sha=self.base)
        with self.assertRaises(InputError):
            self.create_run(campaign={"campaign_id": "old", "task_id": "child"})
        self.reference["epoch"] = True
        with self.assertRaises(InputError):
            self.create_run()

    def test_mcp_get_is_read_only_and_unknown_record_data_is_rejected(self):
        before = self.document["state"]
        fetched = self.server.call_tool("get_followup", {"workspace": str(self.repo.path), "followup_id": "chain"})
        self.assertFalse(fetched["isError"])
        self.assertEqual(before, fetched["structuredContent"]["state"])
        result = self.server.call_tool("record_followup", {
            "workspace": str(self.repo.path), "followup_id": "chain", "owner_id": "lead",
            "expected_revision": before["revision"], "request_id": "invalid", "action": "progress",
            "data": {"bucket": "writing", "duration_ms": 1, "stderr": "private"},
        })
        self.assertTrue(result["isError"])
        self.assertNotIn("private", str(result))


if __name__ == "__main__":
    unittest.main()
