from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support
from agent_harness.service import HarnessService
from agent_harness.util import StateError


class ProviderFailurePersistenceTests(unittest.TestCase):
    def test_request_error_is_persisted_without_retry_fallback_or_sensitive_data(self):
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            fake = _support.make_fake_claude(Path(directory))
            service = HarnessService(_support.fake_environment(fake, FAKE_CLAUDE_MODE="api_request_error"))
            run = service.create_run({"workspace": str(repo.path), "goal": "Update docs",
                                      "done_when": ["Docs updated"], "max_critic_retries": 1})
            args = {"workspace": str(repo.path), "run_id": run["contract"]["run_id"]}
            (repo.path / "README.md").write_text("changed\n", encoding="utf-8")
            service.plan_checks(args)
            service.record_check({**args, "check_name": "git-diff-check", "exit_code": 0, "duration_ms": 1})
            stage = service.start_stage({**args, "profile": "critic"})
            terminal = _support.wait_until(lambda: service.poll_stage({**args, "stage_id": stage["stage_id"]}).get("terminal"))
            self.assertEqual("request_configuration", terminal["failure_kind"])
            saved = service.get_run(args)
            persisted = saved["state"]["stages"][stage["stage_id"]]
            self.assertEqual({"code": "thinking_config_incompatible", "http_status": 400},
                             persisted["telemetry"]["provider_error"])
            with self.assertRaises(StateError):
                service.start_stage({**args, "profile": "critic", "retry_stage_id": stage["stage_id"]})
            with self.assertRaises(StateError):
                service.finish_run({**args, "status": "complete"})
            for path in (repo.path / ".git" / "codex-agent-harness").rglob("*.json*"):
                text = path.read_text(encoding="utf-8")
                for private in ("private-provider-value", "private-session-id", "thinking.type.enabled"):
                    self.assertNotIn(private, text)
            self.assertEqual(1, len(service.get_run(args)["state"]["stages"]))
