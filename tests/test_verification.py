from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support
from agent_harness.git_repo import resolve_repo
from agent_harness.service import HarnessService
from agent_harness.store import RunStore
from agent_harness.util import StateError, InputError
from agent_harness.verification import (
    closeout_paths, correction_context, evidence_identity, optional_snapshot,
    product_evidence, require_fresh_evidence, require_frozen_contract,
    review_snapshot, scope_violations,
)
from test_workflow import run_planned_checks, wait_for_stage
from test_campaign import campaign_arguments, complete_run, task


class VerificationReuseTests(unittest.TestCase):
    def test_push_scope_and_evidence_are_bound_to_head_and_contract(self):
        with _support.TempRepo() as repo:
            context = resolve_repo(repo.path)
            contract = {"goal": "Implement scoped push", "scope_paths": ["internal/push/"]}
            pinned = evidence_identity(context, contract)
            self.assertEqual([], scope_violations(["internal/push/service.go"], contract["scope_paths"]))
            self.assertEqual(["internal/auth/secrets.go"], scope_violations(
                ["internal/push/service.go", "internal/auth/secrets.go"], contract["scope_paths"]
            ))
            require_fresh_evidence(context, contract, pinned)
            require_frozen_contract(context, contract, {"contract_sha256": pinned["contract_sha256"]})
            with self.assertRaisesRegex(StateError, "stale"):
                require_fresh_evidence(context, {**contract, "goal": "Expand push"}, pinned)
            with self.assertRaisesRegex(StateError, "frozen contract"):
                require_frozen_contract(context, {**contract, "goal": "Expand push"}, {"contract_sha256": pinned["contract_sha256"]})
            (repo.path / "README.md").write_text("new commit\n")
            _support.git(repo.path, "add", "README.md")
            _support.git(repo.path, "commit", "-m", "advance head")
            with self.assertRaisesRegex(StateError, "stale"):
                require_fresh_evidence(resolve_repo(repo.path), contract, pinned)

    def test_scope_path_escape_and_unexpected_push_change_are_rejected(self):
        with _support.TempRepo() as repo:
            service = HarnessService({})
            for bad in ["../outside", "/absolute", "internal//push/", "./internal/push/"]:
                with self.subTest(path=bad), self.assertRaises(InputError):
                    service.create_run({
                        "workspace": str(repo.path), "goal": "Scoped push",
                        "done_when": ["Push works"], "scope_paths": [bad],
                    })
            created = service.create_run({
                "workspace": str(repo.path), "goal": "Scoped push",
                "done_when": ["Push works"], "scope_paths": ["internal/push/"],
            })
            (repo.path / "internal" / "push").mkdir(parents=True)
            (repo.path / "internal" / "push" / "service.go").write_text("package push\n")
            (repo.path / "internal" / "auth").mkdir()
            (repo.path / "internal" / "auth" / "secrets.go").write_text("package auth\n")
            with self.assertRaisesRegex(StateError, "scope"):
                service.plan_checks({"workspace": str(repo.path), "run_id": created["contract"]["run_id"]})

    def test_sip_pilot_gate_does_not_require_production(self):
        pilot = {
            "operating_mode": "SIP pilot on test VM",
            "runtime_required": True,
            "external_dependencies": [],
        }
        evidence, blockers = product_evidence(pilot, {
            "runtime": ["Test VM SIP flow observed 180/183 and CANCEL"],
            "external": {},
        })
        self.assertEqual([], blockers)
        self.assertEqual({}, evidence["external"])
        production = {
            "operating_mode": "Production SIP service",
            "runtime_required": True,
            "external_dependencies": ["production deployment"],
        }
        _, blockers = product_evidence(production, {
            "runtime": [],
            "external": {"production deployment": {
                "status": "blocked", "evidence": "Deployment approval pending",
            }},
        })
        self.assertEqual(2, len(blockers))
        self.assertIn("runtime evidence", blockers[0])
        self.assertIn("production deployment", blockers[1])

    def test_planned_check_rejects_head_and_contract_drift(self):
        for drift in ("head", "contract"):
            with self.subTest(drift=drift), _support.TempRepo() as repo:
                service = HarnessService({})
                created = service.create_run({
                    "workspace": str(repo.path), "goal": "Scoped push",
                    "done_when": ["Push works"],
                })
                run_id = created["contract"]["run_id"]
                common = {"workspace": str(repo.path), "run_id": run_id}
                (repo.path / "README.md").write_text("push changed\n")
                planned = service.plan_checks(common)
                if drift == "head":
                    _support.git(repo.path, "add", "README.md")
                    _support.git(repo.path, "commit", "-m", "push change")
                else:
                    store = RunStore.for_workspace(repo.path)
                    contract = store.read_contract(run_id)
                    contract["goal"] = "Changed contract without reapproval"
                    RunStore._atomic_json(store.run_dir(run_id) / "contract.json", contract)
                with self.assertRaisesRegex(StateError, "stale" if drift == "head" else "frozen contract"):
                    service.record_check({
                        **common, "check_name": planned["checks"][0]["name"],
                        "exit_code": 0, "duration_ms": 1,
                    })
                if drift == "head":
                    replanned = service.plan_checks(common)
                    self.assertFalse(replanned["deduplicated"])
                else:
                    with self.assertRaisesRegex(StateError, "frozen contract"):
                        service.plan_checks(common)

    def test_replanned_head_cannot_reuse_old_critic_review(self):
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as temporary:
            service = HarnessService(_support.fake_environment(
                _support.make_fake_claude(Path(temporary)), _support.PASS_REVIEW
            ))
            created = service.create_run({
                "workspace": str(repo.path), "goal": "Update push documentation",
                "done_when": ["Documentation is correct"],
            })
            common = {"workspace": str(repo.path), "run_id": created["contract"]["run_id"]}
            (repo.path / "README.md").write_text("updated\n")
            run_planned_checks(service, repo.path, common["run_id"])
            stage = service.start_stage({**common, "profile": "critic"})
            wait_for_stage(service, repo.path, common["run_id"], stage["stage_id"])
            service.record_review_resolution({**common, "resolutions": []})
            _support.git(repo.path, "add", "README.md")
            _support.git(repo.path, "commit", "-m", "advance reviewed head")
            with self.assertRaisesRegex(StateError, "stale"):
                service.finish_run({**common, "status": "complete"})
            run_planned_checks(service, repo.path, common["run_id"])
            with self.assertRaisesRegex(StateError, "stale"):
                service.finish_run({**common, "status": "complete"})

    def test_fallback_requires_same_head_and_contract_as_limit_stage(self):
        state = {
            "review_cycle": 0,
            "diff_fingerprint": "same-diff",
            "evidence_identity": {"head_sha": "new-head", "contract_sha256": "contract"},
            "stages": {"old-limit": {
                "profile": "critic", "lifecycle_state": "failed",
                "failure_kind": "anthropic_limit", "review_cycle": 1,
                "diff_fingerprint": "same-diff",
                "evidence_identity": {"head_sha": "old-head", "contract_sha256": "contract"},
            }},
        }
        self.assertFalse(HarnessService._codex_fallback_allowed(state))
        state["stages"]["old-limit"]["evidence_identity"] = state["evidence_identity"]
        self.assertTrue(HarnessService._codex_fallback_allowed(state))

    def review_file(self, path="README.md", severity="P3"):
        review = _support.finding_review(severity)
        review["findings"][0]["file"] = path
        return {"review": review, "resolutions": {"F-1": {"disposition": "accepted", "resolved": False}}}

    def test_snapshot_is_content_only_and_indirect_is_narrow(self):
        with _support.TempRepo() as repo:
            module = repo.path / "go.mod"
            module.write_text("module example.invalid/app\nrequire example.invalid/lib v1.0.0 // indirect\n")
            before = review_snapshot(resolve_repo(repo.path))
            module.write_text("module example.invalid/app\nrequire example.invalid/lib v1.0.0\n")
            after = review_snapshot(resolve_repo(repo.path))
            self.assertEqual(["go.mod"], closeout_paths(before, after, self.review_file("go.mod")))
            self.assertNotIn("example.invalid", json.dumps(before))
            module.write_text("module example.invalid/app\nrequire example.invalid/lib v2.0.0\n")
            with self.assertRaises(StateError):
                closeout_paths(before, review_snapshot(resolve_repo(repo.path)), self.review_file("go.mod"))

    def test_snapshot_budget_disables_reuse_without_unbounded_reads(self):
        with _support.TempRepo() as repo:
            with (repo.path / "large.bin").open("wb") as stream:
                stream.truncate(33 * 1024 * 1024)
            self.assertIsNone(optional_snapshot(resolve_repo(repo.path)))
            self.assertEqual("full", correction_context(resolve_repo(repo.path), None, None, {})["mode"])

    def test_staged_only_code_cannot_hide_behind_editorial_worktree_delta(self):
        with _support.TempRepo() as repo:
            code = repo.path / "app.py"
            code.write_text("original = True\n")
            _support.git(repo.path, "add", "app.py")
            _support.git(repo.path, "commit", "-m", "initial code")
            before = review_snapshot(resolve_repo(repo.path))
            code.write_text("unreviewed = True\n")
            _support.git(repo.path, "add", "app.py")
            code.write_text("original = True\n")
            (repo.path / "README.md").write_text("fixed link\n")
            with self.assertRaisesRegex(StateError, "match accepted P3"):
                closeout_paths(before, review_snapshot(resolve_repo(repo.path)), self.review_file())

    def test_staged_document_allowed_but_third_index_version_and_modes_rejected(self):
        with _support.TempRepo() as repo:
            before = review_snapshot(resolve_repo(repo.path))
            readme = repo.path / "README.md"
            readme.write_text("fixed link\n")
            _support.git(repo.path, "add", "README.md")
            after = review_snapshot(resolve_repo(repo.path))
            self.assertEqual(["README.md"], closeout_paths(before, after, self.review_file()))
            readme.write_text("another unstaged version\n")
            with self.assertRaisesRegex(StateError, "index differs"):
                closeout_paths(before, review_snapshot(resolve_repo(repo.path)), self.review_file())
            readme.write_text("fixed link\n")
            _support.git(repo.path, "update-index", "--chmod=+x", "README.md")
            with self.assertRaisesRegex(StateError, "index differs"):
                closeout_paths(before, review_snapshot(resolve_repo(repo.path)), self.review_file())

    def test_staged_module_version_is_not_an_indirect_only_correction(self):
        with _support.TempRepo() as repo:
            module = repo.path / "go.mod"
            original = "module example.invalid/app\nrequire example.invalid/lib v1.0.0 // indirect\n"
            module.write_text(original)
            _support.git(repo.path, "add", "go.mod")
            _support.git(repo.path, "commit", "-m", "initial modules")
            before = review_snapshot(resolve_repo(repo.path))
            module.write_text(original.replace("v1.0.0", "v2.0.0"))
            _support.git(repo.path, "add", "go.mod")
            module.write_text(original.replace(" // indirect", ""))
            with self.assertRaisesRegex(StateError, "index differs"):
                closeout_paths(before, review_snapshot(resolve_repo(repo.path)), self.review_file("go.mod"))

    def test_code_instruction_modes_and_new_paths_require_full_path(self):
        for relative in ["app.py", "README.py", "AGENTS.md", "skills/review/SKILL.md", "docs/contract.md", "config.yaml"]:
            with self.subTest(path=relative), _support.TempRepo() as repo:
                path = repo.path / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("before\n")
                before = review_snapshot(resolve_repo(repo.path))
                path.write_text("after\n")
                with self.assertRaises(StateError):
                    closeout_paths(before, review_snapshot(resolve_repo(repo.path)), self.review_file(relative))
        with _support.TempRepo() as repo:
            before = review_snapshot(resolve_repo(repo.path))
            (repo.path / "README.md").chmod(0o755)
            with self.assertRaises(StateError):
                closeout_paths(before, review_snapshot(resolve_repo(repo.path)), self.review_file())

    def test_closeout_reuses_review_not_checks_and_is_idempotent(self):
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as temporary:
            service = HarnessService(_support.fake_environment(
                _support.make_fake_claude(Path(temporary)), _support.finding_review("P3")
            ))
            created = service.create_run({"workspace": str(repo.path), "goal": "Update docs", "done_when": ["Docs correct"], "max_correction_passes": 0})
            common = {"workspace": str(repo.path), "run_id": created["contract"]["run_id"]}
            (repo.path / "README.md").write_text("broken link\n")
            plan = run_planned_checks(service, repo.path, common["run_id"])
            stage = service.start_stage({**common, "profile": "critic"})
            wait_for_stage(service, repo.path, common["run_id"], stage["stage_id"])
            resolution = {"finding_id": "F-1", "disposition": "accepted", "resolved": False, "evidence": "Repair the documentation link"}
            service.record_review_resolution({**common, "resolutions": [resolution]})
            (repo.path / "README.md").write_text("repaired link\n")
            closeout = service.plan_checks({**common, "nonsemantic_closeout": True})
            self.assertNotEqual(plan["diff_fingerprint"], closeout["diff_fingerprint"])
            with self.assertRaisesRegex(StateError, "required checks"):
                service.finish_run({**common, "status": "complete"})
            run_planned_checks(service, repo.path, common["run_id"])
            with self.assertRaisesRegex(StateError, "not resolved"):
                service.finish_run({**common, "status": "complete"})
            service.record_review_resolution({**common, "resolutions": [{**resolution, "resolved": True}]})
            state = service.get_run(common)
            self.assertNotIn("review_snapshot", state["state"])
            self.assertNotIn("snapshot", state["state"]["stages"][stage["stage_id"]])
            self.assertEqual(1, state["state"]["review_snapshot_summary"]["file_count"])
            self.assertEqual(plan["diff_fingerprint"], state["review"]["review"]["diff_fingerprint"])
            self.assertEqual(0, state["state"]["correction_passes"])
            self.assertEqual(1, len(state["state"]["stages"]))
            # Mutation after measurement cannot be certified by the old checks.
            (repo.path / "app.py").write_text("print('new behavior')\n")
            with self.assertRaises(StateError):
                service.finish_run({**common, "status": "complete"})
            (repo.path / "app.py").unlink()
            finished = service.finish_run({**common, "status": "complete"})
            self.assertEqual("nonsemantic_closeout", finished["terminal"]["verification_reuse"]["kind"])
            self.assertTrue(service.finish_run({**common, "status": "complete"})["deduplicated"])

    def test_legacy_missing_snapshot_and_p2_cannot_closeout(self):
        with _support.TempRepo() as repo:
            before = review_snapshot(resolve_repo(repo.path))
            (repo.path / "README.md").write_text("fixed\n")
            after = review_snapshot(resolve_repo(repo.path))
            for snapshot, review in [(None, self.review_file()), (before, self.review_file(severity="P2"))]:
                with self.assertRaises(StateError):
                    closeout_paths(snapshot, after, review)
            with self.assertRaisesRegex(StateError, "pinned contract"):
                closeout_paths(before, after, self.review_file(), {
                    "contract_refs": [{"ref": "README.md", "revision": "pinned"}]
                })
            (repo.path / "README.md").write_bytes(b"binary\0data")
            with self.assertRaises(StateError):
                closeout_paths(before, review_snapshot(resolve_repo(repo.path)), self.review_file())

    def test_correction_scope_needs_accessible_clean_base_and_preserves_decisions(self):
        with _support.TempRepo() as repo:
            before = review_snapshot(resolve_repo(repo.path))
            (repo.path / "README.md").write_text("correction\n")
            after = review_snapshot(resolve_repo(repo.path))
            scope = correction_context(resolve_repo(repo.path), before, after, self.review_file())
            self.assertEqual("correction", scope["mode"])
            self.assertEqual(["README.md"], scope["changed_since_review"])
            self.assertIn("F-1", scope["previous_resolutions"])
            dirty = {**before, "clean": False}
            self.assertEqual("full", correction_context(resolve_repo(repo.path), dirty, after, self.review_file())["mode"])
            missing = {**before, "head_sha": "0" * 40}
            self.assertEqual("full", correction_context(resolve_repo(repo.path), missing, after, self.review_file())["mode"])
            _support.git(repo.path, "add", "README.md")
            _support.git(repo.path, "commit", "--amend", "-m", "corrected")
            amended = correction_context(resolve_repo(repo.path), before, review_snapshot(resolve_repo(repo.path)), self.review_file())
            self.assertEqual("correction", amended["mode"])

    def test_single_source_wave_and_legacy_integration_policy(self):
        with _support.TempRepo() as repo:
            tasks = [task("A", kind="implementation"), {
                **task("B", kind="implementation", dependencies=["A"]), "wave": 2, "base_from_task": "A"
            }]
            campaign = HarnessService({}).create_campaign(campaign_arguments(repo, tasks=tasks, with_spec=True))
            self.assertEqual([], HarnessService._integration_gaps(campaign["contract"], campaign["state"]))
            legacy = {**campaign["contract"], "integration_policy": "combined-review-required"}
            self.assertEqual(2, len(HarnessService._integration_gaps(legacy, campaign["state"])))

    def test_parallel_wave_cannot_pick_one_source_as_combined_base(self):
        with _support.TempRepo() as repo:
            tasks = [task("A", kind="implementation"), task("B", kind="implementation"), {
                **task("C", kind="implementation", dependencies=["A", "B"]), "wave": 2, "base_from_task": "A"
            }]
            with self.assertRaisesRegex(InputError, "preceding integration wave"):
                HarnessService({}).create_campaign(campaign_arguments(repo, tasks=tasks, with_spec=True))

    def test_sequential_base_uses_verified_sha_and_rejects_stale_candidate(self):
        for stale in [False, True]:
            with self.subTest(stale=stale), _support.TempRepo() as repo:
                service = HarnessService({})
                campaign = service.create_campaign(campaign_arguments(repo, with_spec=True, tasks=[
                    task("A", kind="implementation"), {
                        **task("B", kind="implementation", dependencies=["A"]),
                        "wave": 2, "base_from_task": "A",
                    },
                ]))
                common = {"workspace": str(repo.path), "campaign_id": campaign["contract"]["campaign_id"]}
                service.record_campaign_task({**common, "task_id": "A", "status": "in_progress"})
                run = service.create_run({"workspace": str(repo.path), "campaign": {**common, "task_id": "A"}})
                (repo.path / "README.md").write_text("candidate\n")
                _support.git(repo.path, "add", "README.md")
                _support.git(repo.path, "commit", "-m", "candidate")
                head = _support.git(repo.path, "rev-parse", "HEAD")
                run_id = run["contract"]["run_id"]
                # Изолируем проверку связи задач от проверки завершения прогона.
                complete_run(repo, run_id)
                if stale:
                    (repo.path / "README.md").write_text("unverified addition\n")
                    with self.assertRaisesRegex(StateError, "changed after verification"):
                        service.record_campaign_task({**common, "task_id": "A", "status": "complete", "run_workspace": str(repo.path), "run_id": run_id})
                    continue
                service.record_campaign_task({**common, "task_id": "A", "status": "complete", "run_workspace": str(repo.path), "run_id": run_id})
                service.record_campaign_task({**common, "task_id": "B", "status": "in_progress"})
                following = service.create_run({"workspace": str(repo.path), "campaign": {**common, "task_id": "B"}})
                self.assertEqual(head, following["contract"]["base_sha"])


if __name__ == "__main__":
    unittest.main()
