from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support

from agent_harness.claude_runtime import (
    ManagedStage,
    build_command,
    check_runtime,
)
from agent_harness.service import HarnessService
from agent_harness.util import StateError


class ReadinessTests(unittest.TestCase):
    def test_ready_subscription_runtime_does_not_invoke_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _support.make_fake_claude(root)
            marker = root / "invoked"
            environ = _support.fake_environment(
                fake, FAKE_CLAUDE_MARKER=str(marker)
            )
            report = check_runtime(environ)
            self.assertTrue(report["ok"])
            self.assertFalse(report["model_invoked"])
            self.assertFalse(marker.exists())
            self.assertEqual([], report["billing_guard"]["active_environment"])

    def test_billing_environment_blocks_without_exposing_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _support.make_fake_claude(root)
            marker = root / "invoked"
            environ = _support.fake_environment(
                fake,
                FAKE_CLAUDE_MARKER=str(marker),
                ANTHROPIC_API_KEY="super-secret-value",
            )
            report = check_runtime(environ)
            self.assertFalse(report["ok"])
            self.assertEqual(
                ["ANTHROPIC_API_KEY"],
                report["billing_guard"]["active_environment"],
            )
            self.assertNotIn("super-secret-value", repr(report))
            self.assertFalse(marker.exists())

    def test_missing_safety_flag_blocks_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _support.make_fake_claude(root)
            environ = _support.fake_environment(
                fake, FAKE_CLAUDE_MISSING_FLAG="--safe-mode"
            )
            report = check_runtime(environ)
            self.assertFalse(report["ok"])
            self.assertEqual(
                ["--safe-mode"], report["claude"]["required_flags"]["missing"]
            )

    def test_logged_out_start_never_invokes_model(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = _support.make_fake_claude(root)
            marker = root / "invoked"
            service = HarnessService(
                _support.fake_environment(
                    fake,
                    FAKE_CLAUDE_LOGGED_IN="0",
                    FAKE_CLAUDE_MARKER=str(marker),
                )
            )
            run = service.create_run(
                {
                    "workspace": str(repo.path),
                    "goal": "Update docs",
                    "done_when": ["Docs are current"],
                }
            )
            run_id = run["contract"]["run_id"]
            (repo.path / "README.md").write_text("changed\n", encoding="utf-8")
            service.plan_checks({"workspace": str(repo.path), "run_id": run_id})
            service.record_check(
                {
                    "workspace": str(repo.path),
                    "run_id": run_id,
                    "check_name": "git-diff-check",
                    "exit_code": 0,
                    "duration_ms": 1,
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
            self.assertFalse(marker.exists())


class CommandTests(unittest.TestCase):
    def test_critic_command_is_read_only_and_has_no_fallback(self) -> None:
        command = build_command(
            "/fake/claude", profile="critic", model="claude-opus-5"
        )
        joined = " ".join(command)
        self.assertIn("--permission-mode plan", joined)
        self.assertIn("--tools Read,Glob,Grep,Bash", joined)
        self.assertIn("--disallowedTools Edit,Write,NotebookEdit", joined)
        self.assertIn("--safe-mode", command)
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn("--fallback-model", command)

    def test_implement_command_is_distinct(self) -> None:
        command = build_command(
            "/fake/claude", profile="implement", model="claude-opus-5"
        )
        joined = " ".join(command)
        self.assertIn("--permission-mode auto", joined)
        self.assertIn("Edit,Write", joined)
        self.assertNotIn("--disallowedTools", command)


class ManagedStageTests(unittest.TestCase):
    def _stage(
        self,
        root: Path,
        *,
        mode: str = "success",
        model: str = "claude-opus-5",
        timeout: int = 30,
    ) -> tuple[ManagedStage, list[dict], list[dict]]:
        fake = _support.make_fake_claude(root)
        environ = _support.fake_environment(
            fake,
            _support.PASS_REVIEW,
            FAKE_CLAUDE_MODE=mode,
            FAKE_CLAUDE_MODEL=model,
        )
        events: list[dict] = []
        terminals: list[dict] = []
        stage = ManagedStage(
            stage_id="run-test:critic:1",
            run_id="run-test",
            profile="critic",
            command=build_command(str(fake), profile="critic", model="claude-opus-5"),
            cwd=root,
            prompt="review the repository",
            environ=environ,
            requested_model="claude-opus-5",
            timeout_seconds=timeout,
            heartbeat_seconds=1,
            stall_seconds=5,
            on_event=events.append,
            on_terminal=terminals.append,
        )
        return stage, events, terminals

    def test_stream_exposes_only_allowlisted_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage, events, terminals = self._stage(Path(directory))
            terminal = _support.wait_until(
                lambda: stage.poll().get("terminal")
            )
            self.assertEqual("completed", terminal["lifecycle_state"])
            rendered = repr(events) + repr(terminals)
            self.assertNotIn("tool-secret-id", rendered)
            self.assertNotIn("hidden", rendered)
            self.assertNotIn("discard-me", rendered)
            self.assertNotIn("x" * 100, rendered)
            self.assertIn("assistant_progress", rendered)
            self.assertEqual("pass", terminal["result"]["verdict"])

    def test_stderr_and_invalid_json_are_counted_not_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage, events, terminals = self._stage(Path(directory), mode="fail")
            terminal = _support.wait_until(
                lambda: stage.poll().get("terminal")
            )
            self.assertEqual("failed", terminal["lifecycle_state"])
            rendered = repr(events) + repr(terminals)
            self.assertNotIn("super-secret-value", rendered)
            self.assertGreater(terminal["telemetry"]["provider_stderr_chars"], 0)
            self.assertGreater(terminal["telemetry"]["invalid_lines"], 0)

    def test_anthropic_limit_is_classified_without_exposing_provider_text(self) -> None:
        for mode in ("limit_stderr", "limit_result"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                stage, events, terminals = self._stage(Path(directory), mode=mode)
                terminal = _support.wait_until(
                    lambda: stage.poll().get("terminal")
                )
                self.assertEqual("failed", terminal["lifecycle_state"])
                self.assertEqual("anthropic_limit", terminal["failure_kind"])
                self.assertEqual("Anthropic usage limit reached", terminal["error"])
                rendered = repr(events) + repr(terminals)
                self.assertNotIn("super-secret-value", rendered)
                self.assertNotIn("subscription limit", rendered)

    def test_limit_warning_does_not_override_a_successful_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage, _events, _terminals = self._stage(
                Path(directory), mode="success_limit_warning"
            )
            terminal = _support.wait_until(lambda: stage.poll().get("terminal"))
            self.assertEqual("completed", terminal["lifecycle_state"])

    def test_quality_floor_violation_fails_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage, _events, _terminals = self._stage(
                Path(directory), model="claude-sonnet-5"
            )
            terminal = _support.wait_until(
                lambda: stage.poll().get("terminal")
            )
            self.assertEqual("failed", terminal["lifecycle_state"])
            self.assertEqual(
                "violated", terminal["telemetry"]["quality_floor_status"]
            )

    def test_cancel_does_not_restart_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage, _events, terminals = self._stage(Path(directory), mode="hang")
            terminal = stage.cancel()["terminal"]
            self.assertEqual("interrupted", terminal["lifecycle_state"])
            self.assertEqual(1, len(terminals))

    def test_timeout_is_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage, _events, _terminals = self._stage(
                Path(directory), mode="hang", timeout=1
            )
            terminal = _support.wait_until(
                lambda: stage.poll().get("terminal"), timeout=4
            )
            self.assertEqual("failed", terminal["lifecycle_state"])
            self.assertIn("timed out", terminal["error"])


if __name__ == "__main__":
    unittest.main()
