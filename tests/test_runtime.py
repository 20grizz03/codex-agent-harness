from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support

from agent_harness.claude_runtime import (
    ManagedStage,
    SANDBOX_SETTINGS,
    build_command,
    check_runtime,
)
from agent_harness.service import HarnessService
from agent_harness.util import InputError, StateError


def _initialize_git_repository(root: Path) -> None:
    _support.git(root, "init", "-b", "main")
    _support.git(root, "config", "user.name", "Agent Harness Tests")
    _support.git(root, "config", "user.email", "tests@example.invalid")
    (root / "README.md").write_text("initial\n", encoding="utf-8")
    _support.git(root, "add", "README.md")
    _support.git(root, "commit", "-m", "initial")


def _command_settings(command: list[str]) -> dict:
    return json.loads(command[command.index("--settings") + 1])


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
        with _support.TempRepo() as repo:
            command = build_command(
                "/fake/claude",
                profile="critic",
                model="claude-opus-5",
                cwd=repo.path,
            )
        joined = " ".join(command)
        self.assertIn("--permission-mode plan", joined)
        self.assertIn("--tools Read,Glob,Grep,Bash", joined)
        self.assertIn("--disallowedTools Edit,Write,NotebookEdit", joined)
        self.assertIn("--safe-mode", command)
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual("", command[command.index("--setting-sources") + 1])
        self.assertNotIn("--fallback-model", command)

    def test_critic_command_denies_repository_and_git_metadata_writes(self) -> None:
        with _support.TempRepo() as repo:
            command = build_command(
                "/fake/claude",
                profile="critic",
                model="claude-opus-5",
                cwd=repo.path,
            )

            settings = _command_settings(command)
            self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
            self.assertEqual([], settings["sandbox"]["excludedCommands"])
            self.assertEqual(
                [str(repo.path.resolve()), str((repo.path / ".git").resolve())],
                settings["sandbox"]["filesystem"]["denyWrite"],
            )

    def test_critic_command_denies_linked_worktree_git_metadata_writes(self) -> None:
        with _support.TempRepo() as repo, tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "review-worktree"
            _support.git(repo.path, "worktree", "add", "--detach", str(worktree))
            git_dir = Path(
                _support.git(worktree, "rev-parse", "--absolute-git-dir")
            ).resolve()
            git_common_dir = Path(
                _support.git(
                    worktree,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                )
            ).resolve()

            command = build_command(
                "/fake/claude",
                profile="critic",
                model="claude-opus-5",
                cwd=worktree,
            )

            self.assertEqual(
                [str(worktree.resolve()), str(git_dir), str(git_common_dir)],
                _command_settings(command)["sandbox"]["filesystem"]["denyWrite"],
            )

    def test_critic_command_fails_closed_without_git_workspace(self) -> None:
        with self.assertRaisesRegex(InputError, "requires a Git workspace"):
            build_command(
                "/fake/claude", profile="critic", model="claude-opus-5"
            )

    def test_implement_command_is_distinct(self) -> None:
        command = build_command(
            "/fake/claude", profile="implement", model="claude-opus-5"
        )
        joined = " ".join(command)
        self.assertIn("--permission-mode auto", joined)
        self.assertIn("Edit,Write", joined)
        self.assertNotIn("--disallowedTools", command)
        self.assertEqual(SANDBOX_SETTINGS, _command_settings(command))


class ManagedStageTests(unittest.TestCase):
    def _stage(
        self,
        root: Path,
        *,
        mode: str = "success",
        model: str = "claude-opus-5",
        timeout: int = 30,
    ) -> tuple[ManagedStage, list[dict], list[dict]]:
        _initialize_git_repository(root)
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
            command=build_command(
                str(fake), profile="critic", model="claude-opus-5", cwd=root
            ),
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
            self.assertNotIn("review_normalization", terminal["telemetry"])

    def test_contradictory_review_is_normalized_with_allowlisted_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _initialize_git_repository(root)
            source = _support.finding_review()
            source["verdict"] = "pass"
            fake = _support.make_fake_claude(root)
            environ = _support.fake_environment(fake, source)
            stage = ManagedStage(
                stage_id="run-test:critic:1",
                run_id="run-test",
                profile="critic",
                command=build_command(
                    str(fake), profile="critic", model="claude-opus-5", cwd=root
                ),
                cwd=root,
                prompt="review the repository",
                environ=environ,
                requested_model="claude-opus-5",
                timeout_seconds=30,
                heartbeat_seconds=1,
                stall_seconds=5,
                on_event=lambda _event: None,
                on_terminal=lambda _terminal: None,
            )

            terminal = _support.wait_until(lambda: stage.poll().get("terminal"))

            self.assertEqual("completed", terminal["lifecycle_state"])
            self.assertEqual("changes_requested", terminal["result"]["verdict"])
            self.assertEqual(source["findings"], terminal["result"]["findings"])
            self.assertEqual(
                {
                    "original_verdict": "pass",
                    "final_verdict": "changes_requested",
                    "reason": "findings_present",
                },
                terminal["telemetry"]["review_normalization"],
            )
            self.assertIn("duration_ms", terminal["telemetry"])
            self.assertIn("quality_floor_status", terminal["telemetry"])

    def test_blocking_question_normalization_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _initialize_git_repository(root)
            source = _support.finding_review()
            source["verdict"] = "pass"
            source["blocking_question"] = "Which contract is authoritative?"
            fake = _support.make_fake_claude(root)
            environ = _support.fake_environment(fake, source)
            stage = ManagedStage(
                stage_id="run-test:critic:1",
                run_id="run-test",
                profile="critic",
                command=build_command(
                    str(fake), profile="critic", model="claude-opus-5", cwd=root
                ),
                cwd=root,
                prompt="review the repository",
                environ=environ,
                requested_model="claude-opus-5",
                timeout_seconds=30,
                heartbeat_seconds=1,
                stall_seconds=5,
                on_event=lambda _event: None,
                on_terminal=lambda _terminal: None,
            )

            terminal = _support.wait_until(lambda: stage.poll().get("terminal"))

            self.assertEqual("completed", terminal["lifecycle_state"])
            self.assertEqual("blocked", terminal["result"]["verdict"])
            self.assertEqual(source["findings"], terminal["result"]["findings"])
            self.assertEqual(
                "blocking_question_present",
                terminal["telemetry"]["review_normalization"]["reason"],
            )

    def test_malformed_review_remains_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _initialize_git_repository(root)
            source = _support.finding_review()
            source["verdict"] = "pass"
            source["findings"][0]["severity"] = "P4"
            fake = _support.make_fake_claude(root)
            environ = _support.fake_environment(fake, source)
            stage = ManagedStage(
                stage_id="run-test:critic:1",
                run_id="run-test",
                profile="critic",
                command=build_command(
                    str(fake), profile="critic", model="claude-opus-5", cwd=root
                ),
                cwd=root,
                prompt="review the repository",
                environ=environ,
                requested_model="claude-opus-5",
                timeout_seconds=30,
                heartbeat_seconds=1,
                stall_seconds=5,
                on_event=lambda _event: None,
                on_terminal=lambda _terminal: None,
            )

            terminal = _support.wait_until(lambda: stage.poll().get("terminal"))

            self.assertEqual("failed", terminal["lifecycle_state"])
            self.assertIn("finding.severity", terminal["error"])
            self.assertEqual("invalid_output", terminal["failure_kind"])
            self.assertNotIn("review_normalization", terminal["telemetry"])

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
        for mode in ("limit_stderr", "limit_result", "limit_after_safety"):
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

    def test_terminal_remains_observable_when_callback_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _initialize_git_repository(root)
            fake = _support.make_fake_claude(root)

            def fail_to_persist(_terminal: dict) -> None:
                raise RuntimeError("persistence failed")

            stage = ManagedStage(
                stage_id="run-test:critic:1",
                run_id="run-test",
                profile="critic",
                command=build_command(
                    str(fake), profile="critic", model="claude-opus-5", cwd=root
                ),
                cwd=root,
                prompt="review the repository",
                environ=_support.fake_environment(fake, _support.PASS_REVIEW),
                requested_model="claude-opus-5",
                timeout_seconds=30,
                heartbeat_seconds=1,
                stall_seconds=5,
                on_event=lambda _event: None,
                on_terminal=fail_to_persist,
            )

            terminal = _support.wait_until(lambda: stage.poll().get("terminal"))
            self.assertEqual("completed", terminal["lifecycle_state"])

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
            self.assertEqual("transient_timeout", terminal["failure_kind"])

    def test_auth_and_missing_result_are_not_retryable_failures(self) -> None:
        for mode, expected in (
            ("auth_fail", "authentication"),
            ("missing_result", "invalid_output"),
            ("fail", "process_failure"),
            ("transient_fail", "transient_process_failure"),
            ("transient_result_error", "transient_process_failure"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                stage, _events, _terminals = self._stage(Path(directory), mode=mode)
                terminal = _support.wait_until(lambda: stage.poll().get("terminal"))
                self.assertEqual("failed", terminal["lifecycle_state"])
                self.assertEqual(expected, terminal["failure_kind"])


if __name__ == "__main__":
    unittest.main()
