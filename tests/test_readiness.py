from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _support
from agent_harness import claude_runtime as runtime
from agent_harness.service import HarnessService
from agent_harness.util import StateError


class ReadinessDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fake = _support.make_fake_claude(self.root)
        self.marker = self.root / "model-invoked"
        self.environ = _support.fake_environment(
            self.fake, FAKE_CLAUDE_MARKER=str(self.marker)
        )

    def capture(self, *, auth='{"loggedIn":true}', failed=None, exit_code=7,
                version="2.1.280 (Claude Code)"):
        def result(command, **kwargs):
            name = "auth" if command[1] == "auth" else command[1][2:]
            outputs = {
                "version": version,
                "auth": auth,
                "help": " ".join(runtime.REQUIRED_FLAGS),
            }
            return subprocess.CompletedProcess(
                command, exit_code if name == failed else 0,
                outputs[name], "private-diagnostic-value" if failed else "",
            )
        return result

    def test_model_minimum_version_without_inference(self) -> None:
        for version, model, expected in (
            ("2.1.241 (Claude Code)", "claude-opus-5-5", "cli_model_incompatible"),
            ("2.1.279 (Claude Code)", "claude-opus-5-5[1m]", "cli_model_incompatible"),
            ("2.1.280 (Claude Code)", "claude-opus-5-5", None),
            ("2.2.0 (Claude Code)", "claude-opus-5-5", None),
            ("2.1.241 (Claude Code)", "claude-opus-5", None),
            ("2.1.218 (Claude Code)", "claude-opus-5", "cli_model_incompatible"),
            ("untrusted-version-value", "claude-opus-5-5", "cli_version_unknown"),
            ("2.1.280-beta (Claude Code)", "claude-opus-5-5", "cli_version_unknown"),
        ):
            with self.subTest(version=version, model=model), patch.object(
                runtime, "_capture", side_effect=self.capture(version=version)
            ):
                report = runtime.check_runtime({**self.environ, "AGENT_HARNESS_CLAUDE_MODEL": model})
                self.assertEqual(expected, report.get("error_code"))
                self.assertEqual(expected is None, report["ok"])
                self.assertFalse(report["model_invoked"])
                self.assertNotIn("untrusted-version-value", repr(report))
                self.assertFalse(self.marker.exists())

    def test_process_failures_are_not_reported_as_logout(self) -> None:
        for name in ("version", "auth", "help"):
            with self.subTest(command=name), patch.object(
                runtime, "_capture", side_effect=self.capture(failed=name)
            ):
                report = runtime.check_runtime(self.environ)
                self.assertFalse(report["ok"])
                self.assertEqual("cli_command_failed", report["error_code"])
                self.assertEqual(name, report["failed_check"])
                self.assertEqual(7, report["exit_code"])
                self.assertNotIn("claude auth login", report["error"])
                self.assertNotIn("private-diagnostic-value", repr(report))
                self.assertFalse(self.marker.exists())

    def test_spawn_and_timeout_errors_are_safe_structured_results(self) -> None:
        cases = [
            (PermissionError("private-diagnostic-value"), "cli_spawn_failed"),
            (FileNotFoundError("private-diagnostic-value"), "cli_spawn_failed"),
            (subprocess.TimeoutExpired(["private-diagnostic-value"], 15,
                                       output="private-diagnostic-value"), "cli_timeout"),
        ]
        for error, expected in cases:
            with self.subTest(code=expected), patch.object(runtime, "_capture", side_effect=error):
                report = runtime.check_runtime(self.environ)
                self.assertEqual(expected, report["error_code"])
                self.assertIsNone(report["claude"]["auth"])
                self.assertNotIn("private-diagnostic-value", repr(report))
                self.assertFalse(report["model_invoked"])

    def test_invalid_auth_response_never_implies_logout_or_readiness(self) -> None:
        for auth in ("", "private-diagnostic-value", "[]", "null", "{}",
                     '{"loggedIn":"true"}', '{"loggedIn":1}', '{"loggedIn":null}'):
            with self.subTest(auth=auth), patch.object(
                runtime, "_capture", side_effect=self.capture(auth=auth)
            ):
                report = runtime.check_runtime(self.environ)
                self.assertFalse(report["ok"])
                self.assertEqual("auth_invalid_response", report["error_code"])
                self.assertIsNone(report["claude"]["auth"])
                self.assertNotIn("claude auth login", report["error"])
                self.assertNotIn("private-diagnostic-value", repr(report))

    def test_explicit_logout_accepts_cli_exit_zero_or_one(self) -> None:
        for code in (0, 1):
            with self.subTest(code=code), patch.object(
                runtime, "_capture", side_effect=self.capture(
                    auth='{"loggedIn":false}', failed="auth", exit_code=code
                )
            ):
                report = runtime.check_runtime(self.environ)
                self.assertEqual("authentication_required", report["error_code"])
                self.assertIs(False, report["claude"]["auth"]["loggedIn"])
                self.assertIn("claude auth login", report["error"])

    def test_exit_one_with_logged_in_true_remains_a_command_failure(self) -> None:
        with patch.object(runtime, "_capture", side_effect=self.capture(
            failed="auth", exit_code=1
        )):
            report = runtime.check_runtime(self.environ)
        self.assertEqual("cli_command_failed", report["error_code"])
        self.assertIsNone(report["claude"]["auth"])

    def test_missing_cli_and_temp_directory_failure_do_not_invoke_model(self) -> None:
        with patch.object(runtime, "resolve_claude_bin", return_value=None):
            self.assertEqual("cli_not_found", runtime.check_runtime(self.environ)["error_code"])
        with patch.object(runtime.tempfile, "TemporaryDirectory", side_effect=OSError("private-diagnostic-value")):
            report = runtime.check_runtime(self.environ)
        self.assertEqual("cli_spawn_failed", report["error_code"])
        self.assertEqual("workspace", report["failed_check"])
        self.assertNotIn("private-diagnostic-value", repr(report))
        self.assertFalse(self.marker.exists())

    def test_billing_guard_blocks_before_launching_cli(self) -> None:
        with patch.object(runtime, "_capture") as capture:
            report = runtime.check_runtime({**self.environ, "ANTHROPIC_API_KEY": "private-diagnostic-value"})
        capture.assert_not_called()
        self.assertEqual("billing_environment", report["error_code"])
        self.assertNotIn("private-diagnostic-value", repr(report))

    def test_readiness_uses_one_private_cwd_without_changing_parent(self) -> None:
        parent = Path.cwd()
        seen = []
        original = runtime._capture

        def capture(command, **kwargs):
            cwd = Path(kwargs["cwd"])
            self.assertTrue(cwd.is_dir())
            self.assertEqual(0o700, cwd.stat().st_mode & 0o777)
            self.assertEqual(parent, Path.cwd())
            seen.append(cwd)
            return original(command, **kwargs)

        with patch.object(runtime, "_capture", side_effect=capture):
            report = runtime.check_runtime(self.environ)
        self.assertTrue(report["ok"])
        self.assertEqual(3, len(seen))
        self.assertEqual(1, len(set(seen)))
        self.assertFalse(seen[0].exists())
        self.assertFalse(self.marker.exists())

    @unittest.skipUnless(os.name == "posix", "deleted cwd is a POSIX scenario")
    def test_removed_parent_cwd_does_not_break_cli_readiness(self) -> None:
        vanished = self.root / "removed-cwd"
        vanished.mkdir()
        script = (
            "import os,sys,json; sys.path.insert(0, sys.argv[1]); "
            "from agent_harness.claude_runtime import check_runtime; "
            "os.chdir(sys.argv[2]); os.rmdir(sys.argv[2]); "
            "print(json.dumps(check_runtime()))"
        )
        child = subprocess.run(
            [sys.executable, "-c", script, str(_support.SRC), str(vanished)],
            env={**self.environ, "FAKE_CLAUDE_REQUIRE_CWD": "1"},
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(0, child.returncode, child.stderr)
        self.assertTrue(json.loads(child.stdout)["ok"], child.stdout)
        self.assertFalse(self.marker.exists())

    def test_relative_path_executable_is_resolved_before_changing_child_cwd(self) -> None:
        relative = os.path.relpath(self.root, Path.cwd())
        target = self.root / "claude"
        target.symlink_to(self.fake)
        environ = {**self.environ, "PATH": relative, "AGENT_HARNESS_CLAUDE_BIN": ""}
        report = runtime.check_runtime(environ)
        self.assertTrue(report["ok"], report)
        self.assertTrue(Path(report["claude"]["path"]).is_absolute())

    def test_preflight_failure_preserves_run_and_critic_attempt(self) -> None:
        with _support.TempRepo() as repo:
            service = HarnessService(self.environ)
            run = service.create_run({
                "workspace": str(repo.path), "goal": "Update docs", "done_when": ["Docs updated"]
            })
            args = {"workspace": str(repo.path), "run_id": run["contract"]["run_id"]}
            (repo.path / "README.md").write_text("changed\n", encoding="utf-8")
            service.plan_checks(args)
            service.record_check({**args, "check_name": "git-diff-check", "exit_code": 0, "duration_ms": 1})
            before = service.get_run(args)["state"]
            with patch.object(runtime, "_capture", side_effect=self.capture(version="2.1.241 (Claude Code)")):
                with self.assertRaises(StateError):
                    service.start_stage({**args, "profile": "critic"})
            self.assertEqual(before, service.get_run(args)["state"])
            self.assertFalse(self.marker.exists())
            stage = service.start_stage({**args, "profile": "critic"})
            terminal = _support.wait_until(
                lambda: service.poll_stage({**args, "stage_id": stage["stage_id"]}).get("terminal")
            )
            self.assertEqual("completed", terminal["lifecycle_state"])
            self.assertTrue(stage["stage_id"].endswith(":critic:1"))


if __name__ == "__main__":
    unittest.main()
