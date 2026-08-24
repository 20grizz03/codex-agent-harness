from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "codex-agent-harness"
SRC = PLUGIN_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=str(repo),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


class TempRepo:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name)
        git(self.path, "init", "-b", "main")
        git(self.path, "config", "user.name", "Agent Harness Tests")
        git(self.path, "config", "user.email", "tests@example.invalid")
        (self.path / "README.md").write_text("initial\n", encoding="utf-8")
        git(self.path, "add", "README.md")
        git(self.path, "commit", "-m", "initial")

    def close(self) -> None:
        self.temporary.cleanup()

    def __enter__(self) -> "TempRepo":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


PASS_REVIEW = {
    "verdict": "pass",
    "findings": [],
    "residual_risks": [],
    "blocking_question": None,
}


def finding_review(severity: str = "P2") -> dict[str, Any]:
    return {
        "verdict": "changes_requested",
        "findings": [
            {
                "id": "F-1",
                "severity": severity,
                "file": "README.md",
                "line": 1,
                "title": "Incorrect observable behavior",
                "impact": "The documented behavior is wrong.",
                "evidence": "README.md:1 contradicts the task contract.",
                "fix": "Correct the line and rerun required checks.",
            }
        ],
        "residual_risks": [],
        "blocking_question": None,
    }


IMPLEMENT_RESULT = {
    "summary": "Implemented the requested local change.",
    "changed_paths": ["README.md"],
    "checks_run": [],
    "risks": [],
    "blockers": [],
}


def make_fake_claude(directory: Path) -> Path:
    executable = directory / "fake-claude"
    script = f"""#!{sys.executable}
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.220 (fake)")
    raise SystemExit(0)
if args == ["auth", "status", "--json"]:
    print(json.dumps({{
        "loggedIn": os.environ.get("FAKE_CLAUDE_LOGGED_IN", "1") == "1",
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
    }}))
    raise SystemExit(0)
if args == ["--help"]:
    flags = [
        "--safe-mode", "--no-session-persistence",
        "--exclude-dynamic-system-prompt-sections", "--output-format",
        "--include-partial-messages", "--verbose", "--permission-mode",
        "--settings", "--strict-mcp-config", "--mcp-config", "--no-chrome",
        "--disable-slash-commands", "--json-schema", "--disallowedTools",
    ]
    missing = os.environ.get("FAKE_CLAUDE_MISSING_FLAG", "")
    print(" ".join(flag for flag in flags if flag != missing))
    raise SystemExit(0)
if "-p" not in args:
    raise SystemExit(2)

marker = os.environ.get("FAKE_CLAUDE_MARKER")
if marker:
    Path(marker).write_text("invoked", encoding="utf-8")
mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
if mode == "hang":
    time.sleep(60)
if mode == "fail":
    print("api_key=super-secret-value", file=sys.stderr)
    print("not-json")
    raise SystemExit(7)
if mode == "limit_stderr":
    print("api_key=super-secret-value You've hit your usage limit", file=sys.stderr)
    raise SystemExit(7)
if mode == "limit_result":
    print(json.dumps({{
        "type": "result",
        "subtype": "usage_cap_reached",
        "is_error": True,
        "result": "You've reached your subscription limit",
    }}), flush=True)
    raise SystemExit(1)
if mode == "success_limit_warning":
    print("The service is rate limiting your requests", file=sys.stderr)
write_path = os.environ.get("FAKE_CLAUDE_WRITE_PATH")
if write_path:
    Path(write_path).write_text(
        os.environ.get("FAKE_CLAUDE_WRITE_CONTENT", "written by fake claude\\n"),
        encoding="utf-8",
    )
print(json.dumps({{"type": "system", "subtype": "init", "model": os.environ.get("FAKE_CLAUDE_MODEL", "claude-opus-5")}}), flush=True)
print(json.dumps({{"type": "stream_event", "event": {{"type": "content_block_start", "content_block": {{"type": "tool_use", "name": "Read", "id": "tool-secret-id", "input": {{"token": "hidden"}}}}}}}}), flush=True)
print(json.dumps({{"type": "stream_event", "event": {{"type": "content_block_delta", "delta": {{"type": "text_delta", "text": "x" * 600}}}}}}), flush=True)
result = json.loads(os.environ["FAKE_CLAUDE_RESULT"])
print(json.dumps({{
    "type": "result",
    "structured_output": result,
    "model": os.environ.get("FAKE_CLAUDE_MODEL", "claude-opus-5"),
    "effort": "high",
    "duration_ms": 25,
    "num_turns": 2,
    "usage": {{"input_tokens": 10, "output_tokens": 20, "secret": "discard-me"}},
}}), flush=True)
"""
    executable.write_text(script, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def fake_environment(
    fake_claude: Path,
    result: dict[str, Any] | None = None,
    **updates: str,
) -> dict[str, str]:
    environ = dict(os.environ)
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ):
        environ.pop(name, None)
    environ.update(
        {
            "AGENT_HARNESS_CLAUDE_BIN": str(fake_claude),
            "AGENT_HARNESS_CLAUDE_MODEL": "claude-opus-5",
            "AGENT_HARNESS_TIMEOUT_SECONDS": "30",
            "AGENT_HARNESS_HEARTBEAT_SECONDS": "1",
            "AGENT_HARNESS_STALL_SECONDS": "5",
            "FAKE_CLAUDE_RESULT": json.dumps(result or PASS_REVIEW),
        }
    )
    environ.update(updates)
    return environ


def wait_until(
    predicate: Callable[[], Any], timeout: float = 5.0, interval: float = 0.02
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition did not become true before timeout")
