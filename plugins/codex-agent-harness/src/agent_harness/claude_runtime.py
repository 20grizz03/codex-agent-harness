"""Subscription-only Claude Code readiness and sanitized stage lifecycle."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .git_repo import resolve_repo
from .review import (
    CRITIC_SYSTEM_PROMPT,
    IMPLEMENT_JSON_SCHEMA,
    IMPLEMENT_SYSTEM_PROMPT,
    REVIEW_JSON_SCHEMA,
    normalize_usage,
    validate_implementation_result,
    validate_review_with_normalization,
)
from .util import InputError, numeric_tree, sanitize_text, utc_now


DEFAULT_MODEL = "claude-opus-5"
BILLING_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
REQUIRED_FLAGS = (
    "--safe-mode",
    "--no-session-persistence",
    "--exclude-dynamic-system-prompt-sections",
    "--output-format",
    "--include-partial-messages",
    "--verbose",
    "--permission-mode",
    "--settings",
    "--setting-sources",
    "--strict-mcp-config",
    "--mcp-config",
    "--no-chrome",
    "--disable-slash-commands",
    "--json-schema",
    "--disallowedTools",
)
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
ANTHROPIC_LIMIT_LABELS = {
    "quota_exceeded",
    "quota_reached",
    "rate_limit",
    "rate_limit_error",
    "rate_limited",
    "usage_cap_reached",
    "usage_limit_reached",
}
ANTHROPIC_LIMIT_TEXT_RE = re.compile(
    r"(?:"
    r"\byou(?:'ve| have)\s+(?:hit|reached)\s+(?:your\s+)?[^\r\n]{0,40}\blimit\b"
    r"|\b(?:rate|usage|credit)\s+limit\s+(?:has\s+been\s+|is\s+)?(?:reached|exceeded)\b"
    r"|\bquota\s+(?:has\s+been\s+|is\s+)?(?:reached|exceeded)\b"
    r"|\brate limiting your requests\b"
    r")",
    re.IGNORECASE,
)
NON_RETRYABLE_FAILURE_PATTERNS = (
    (
        "authentication",
        re.compile(
            r"\b(?:authenticat(?:e|ed|es|ing|ion)|authoriz(?:e|ed|ation)|"
            r"login|logged out|invalid token)\b",
            re.I,
        ),
    ),
    (
        "billing",
        re.compile(r"\b(?:billing|payment|credit card|api key billing)\b", re.I),
    ),
    (
        "safety",
        re.compile(
            r"\b(?:permission denied|sandbox|safety|policy violation)\b", re.I
        ),
    ),
)
TRANSIENT_PROCESS_FAILURE_RE = re.compile(
    r"\b(?:connection (?:reset|refused|closed)|temporarily unavailable|"
    r"temporary service failure|service unavailable|gateway timeout|"
    r"transport error)\b",
    re.I,
)

SANDBOX_SETTINGS = {
    "sandbox": {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
    },
    "autoMemoryEnabled": False,
    "attribution": {"commit": "", "pr": ""},
}


def _truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() not in {"", "0", "false", "no", "off"})


def active_billing_environment(environ: Mapping[str, str]) -> list[str]:
    return [name for name in BILLING_ENV_VARS if _truthy(environ.get(name))]


def resolve_claude_bin(environ: Mapping[str, str]) -> str | None:
    override = environ.get("AGENT_HARNESS_CLAUDE_BIN", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return str(candidate.resolve()) if candidate.is_file() else None
    return shutil.which("claude", path=environ.get("PATH"))


def resolve_model(environ: Mapping[str, str]) -> str:
    model = environ.get("AGENT_HARNESS_CLAUDE_MODEL", DEFAULT_MODEL).strip()
    lowered = model.lower()
    major_match = re.search(r"opus(?:[-_.a-z]*)?[-_.]?([0-9]+)", lowered)
    if "opus" not in lowered:
        raise InputError("AGENT_HARNESS_CLAUDE_MODEL must select Claude Opus")
    if major_match and int(major_match.group(1)) < 5:
        raise InputError("Agent Harness requires Claude Opus 5 or newer")
    if not model or len(model) > 128 or "\x00" in model:
        raise InputError("configured Claude model is invalid")
    return model


def _process_group_kwargs() -> dict[str, Any]:
    return {"start_new_session": True} if os.name == "posix" else {}


def terminate_process(process: subprocess.Popen[str], grace_seconds: float = 2.0) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    process.wait()


def _capture(
    command: Sequence[str],
    *,
    environ: Mapping[str, str],
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            env=dict(environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            **_process_group_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InputError(f"Claude Code readiness check failed: {exc}") from exc


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value.strip())
    except json.JSONDecodeError as exc:
        raise InputError("Claude Code returned invalid JSON for auth status") from exc
    if not isinstance(parsed, dict):
        raise InputError("Claude Code auth status must be a JSON object")
    return parsed


def check_runtime(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    effective = dict(os.environ if environ is None else environ)
    billing = active_billing_environment(effective)
    try:
        model = resolve_model(effective)
    except InputError as exc:
        model = effective.get("AGENT_HARNESS_CLAUDE_MODEL", DEFAULT_MODEL)
        model_error = str(exc)
    else:
        model_error = None
    claude_bin = resolve_claude_bin(effective)
    report: dict[str, Any] = {
        "ok": False,
        "claude": {"path": claude_bin, "version": None, "auth": None},
        "billing_guard": {
            "active_environment": billing,
            "api_billing_allowed": False,
        },
        "profile": {
            "requested_model": model,
            "requested_effort": "high",
            "critic_permission_mode": "plan",
            "implement_permission_mode": "auto",
        },
        "model_invoked": False,
    }
    if not claude_bin:
        report["error"] = "Claude Code CLI was not found"
        return report
    version = _capture([claude_bin, "--version"], environ=effective)
    auth = _capture([claude_bin, "auth", "status", "--json"], environ=effective)
    help_result = _capture([claude_bin, "--help"], environ=effective)
    try:
        auth_object = _json_object(auth.stdout)
    except InputError:
        auth_object = {"loggedIn": False, "authMethod": "unknown"}
    public_auth = {
        key: auth_object.get(key)
        for key in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")
        if key in auth_object
    }
    help_text = f"{help_result.stdout}\n{help_result.stderr}"
    missing_flags = [flag for flag in REQUIRED_FLAGS if flag not in help_text]
    report["claude"] = {
        "path": claude_bin,
        "version": sanitize_text(
            version.stdout or version.stderr, maximum=200
        ),
        "auth": public_auth,
        "required_flags": {
            "ok": help_result.returncode == 0 and not missing_flags,
            "missing": missing_flags,
        },
    }
    if model_error:
        report["error"] = model_error
    elif billing:
        report["error"] = (
            "API/provider billing environment is active; inference is refused"
        )
    elif not bool(public_auth.get("loggedIn")):
        report["error"] = "Claude Code is not signed in; run claude auth login"
    elif version.returncode != 0 or auth.returncode != 0:
        report["error"] = "Claude Code readiness commands failed"
    elif missing_flags or help_result.returncode != 0:
        report["error"] = "Claude Code is missing required safety flags"
    else:
        report["ok"] = True
    return report


def _bounded_seconds(
    environ: Mapping[str, str], name: str, default: int, minimum: int, maximum: int
) -> int:
    raw = environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise InputError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise InputError(f"{name} must be between {minimum} and {maximum}")
    return value


def runtime_timing(environ: Mapping[str, str]) -> tuple[int, int, int]:
    timeout = _bounded_seconds(
        environ, "AGENT_HARNESS_TIMEOUT_SECONDS", 1800, 30, 14_400
    )
    heartbeat = _bounded_seconds(
        environ, "AGENT_HARNESS_HEARTBEAT_SECONDS", 15, 1, 300
    )
    stall = _bounded_seconds(
        environ, "AGENT_HARNESS_STALL_SECONDS", 120, 5, 1_800
    )
    return timeout, heartbeat, stall


def anthropic_cooldown_seconds(environ: Mapping[str, str]) -> int:
    return _bounded_seconds(
        environ,
        "AGENT_HARNESS_ANTHROPIC_COOLDOWN_SECONDS",
        3600,
        1,
        14_400,
    )


def build_command(
    claude_bin: str,
    *,
    profile: str,
    model: str,
    cwd: str | Path | None = None,
) -> list[str]:
    if profile == "critic":
        if cwd is None:
            raise InputError("critic profile requires a Git workspace")
        context = resolve_repo(cwd)
        permission_mode = "plan"
        tools = "Read,Glob,Grep,Bash"
        denied = "Edit,Write,NotebookEdit"
        schema = REVIEW_JSON_SCHEMA
        system_prompt = CRITIC_SYSTEM_PROMPT
        sandbox_settings = {
            **SANDBOX_SETTINGS,
            "sandbox": {
                **SANDBOX_SETTINGS["sandbox"],
                "excludedCommands": [],
                "filesystem": {
                    "denyWrite": list(
                        dict.fromkeys(
                            str(path)
                            for path in (
                                context.repo_root,
                                context.git_dir,
                                context.git_common_dir,
                            )
                        )
                    )
                },
            },
        }
    elif profile == "implement":
        permission_mode = "auto"
        tools = "Read,Glob,Grep,Edit,Write,Bash"
        denied = ""
        schema = IMPLEMENT_JSON_SCHEMA
        system_prompt = IMPLEMENT_SYSTEM_PROMPT
        sandbox_settings = SANDBOX_SETTINGS
    else:
        raise InputError("profile must be critic or implement")
    command = [
        claude_bin,
        "-p",
        "--model",
        model,
        "--effort",
        "high",
        "--permission-mode",
        permission_mode,
        "--tools",
        tools,
        "--settings",
        json.dumps(sandbox_settings, separators=(",", ":")),
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--no-session-persistence",
        "--exclude-dynamic-system-prompt-sections",
        "--safe-mode",
        "--no-chrome",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--name",
        f"codex-agent-harness-{profile}",
        "--append-system-prompt",
        system_prompt,
    ]
    if denied:
        command.extend(["--disallowedTools", denied])
    if profile == "critic":
        command.extend(["--setting-sources", ""])
    return command


def _safe_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if SAFE_LABEL_RE.fullmatch(normalized) else None


def _is_anthropic_limit(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    label = value.strip().casefold().replace("-", "_").replace(" ", "_")
    return label in ANTHROPIC_LIMIT_LABELS or bool(
        ANTHROPIC_LIMIT_TEXT_RE.search(value)
    )


def _result_is_anthropic_limit(payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    return any(
        _is_anthropic_limit(payload.get(key))
        for key in ("subtype", "error_type", "code", "result", "error", "message")
    )


def _non_retryable_failure(value: Any) -> str | None:
    try:
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True)
    except (TypeError, ValueError):
        return None
    for kind, pattern in NON_RETRYABLE_FAILURE_PATTERNS:
        if pattern.search(rendered[-4096:]):
            return kind
    return None


def _transient_process_failure(value: Any) -> bool:
    try:
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True)
    except (TypeError, ValueError):
        return False
    return bool(TRANSIENT_PROCESS_FAILURE_RE.search(rendered[-4096:]))


def _actual_models(payload: Mapping[str, Any]) -> list[str]:
    candidates: list[str] = []
    direct = _safe_label(payload.get("model"))
    if direct:
        candidates.append(direct)
    usage = payload.get("modelUsage")
    if isinstance(usage, Mapping):
        candidates.extend(
            model for key in usage if (model := _safe_label(str(key)))
        )
    return list(dict.fromkeys(candidates))


def _quality_floor(models: list[str]) -> str:
    if not models:
        return "unknown"
    for model in models:
        lowered = model.lower()
        if "opus" not in lowered:
            return "violated"
        match = re.search(r"opus(?:[-_.a-z]*)?[-_.]?([0-9]+)", lowered)
        if match and int(match.group(1)) < 5:
            return "violated"
    return "met"


class ManagedStage:
    """One process with allowlisted progress and a single terminal result."""

    def __init__(
        self,
        *,
        stage_id: str,
        run_id: str,
        profile: str,
        command: Sequence[str],
        cwd: Path,
        prompt: str,
        environ: Mapping[str, str],
        requested_model: str,
        timeout_seconds: int,
        heartbeat_seconds: int,
        stall_seconds: int,
        on_event: Callable[[dict[str, Any]], None],
        on_terminal: Callable[[dict[str, Any]], None],
    ) -> None:
        self.stage_id = stage_id
        self.run_id = run_id
        self.profile = profile
        self.requested_model = requested_model
        self._prompt = prompt
        self._timeout = timeout_seconds
        self._heartbeat = heartbeat_seconds
        self._stall = stall_seconds
        self._on_event = on_event
        self._on_terminal = on_terminal
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = threading.RLock()
        self._terminal_ready = threading.Event()
        self._terminal: dict[str, Any] | None = None
        self._cancelled = False
        self._timed_out = False
        self._result_payload: dict[str, Any] | None = None
        self._failure_kind: str | None = None
        self._limit_seen = False
        self._stderr_scan_tail = ""
        self._stdout_chars = 0
        self._stderr_chars = 0
        self._invalid_lines = 0
        self._assistant_chars = 0
        self._tool_started = 0
        self._tool_completed = 0
        self._tool_names: set[str] = set()
        self._started_monotonic = time.monotonic()
        self._last_provider_event = self._started_monotonic
        self.process, self._launch_attempts = self._spawn(command, cwd, environ)
        self._emit({"type": "stage_started", "profile": profile})
        self._threads = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
            threading.Thread(target=self._write_prompt, daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        threading.Thread(target=self._wait, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    @staticmethod
    def _spawn(
        command: Sequence[str], cwd: Path, environ: Mapping[str, str]
    ) -> tuple[subprocess.Popen[str], int]:
        last_error: OSError | None = None
        for attempt in range(1, 3):
            try:
                return (
                    subprocess.Popen(
                        list(command),
                        cwd=str(cwd),
                        env=dict(environ),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        **_process_group_kwargs(),
                    ),
                    attempt,
                )
            except OSError as exc:
                last_error = exc
        raise InputError(f"Claude Code could not start: {last_error}")

    def _emit(self, event: dict[str, Any]) -> None:
        safe = {"stage_id": self.stage_id, "at": utc_now(), **event}
        self._events.put(dict(safe))
        self._on_event(dict(safe))

    def _write_prompt(self) -> None:
        if self.process.stdin is None:
            return
        try:
            self.process.stdin.write(self._prompt)
            self.process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        finally:
            self._prompt = ""

    def _record_tool(self, event_type: str, block: Mapping[str, Any]) -> None:
        name = _safe_label(block.get("name"))
        if name:
            self._tool_names.add(name)
        if event_type == "started":
            self._tool_started += 1
        else:
            self._tool_completed += 1
        self._emit(
            {
                "type": "tool_activity",
                "started": self._tool_started,
                "completed": self._tool_completed,
                "tools": sorted(self._tool_names)[:12],
            }
        )

    def _consume(self, payload: Mapping[str, Any]) -> None:
        kind = payload.get("type")
        if kind == "system" and payload.get("subtype") == "init":
            model = _safe_label(payload.get("model"))
            event: dict[str, Any] = {"type": "provider_started"}
            if model:
                event["actual_model"] = model
            self._emit(event)
        elif kind == "stream_event":
            event = payload.get("event")
            if not isinstance(event, Mapping):
                return
            if event.get("type") == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, Mapping) and delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str):
                        self._assistant_chars += len(text)
                        if self._assistant_chars % 512 < len(text):
                            self._emit(
                                {
                                    "type": "assistant_progress",
                                    "characters": self._assistant_chars,
                                }
                            )
            elif event.get("type") == "content_block_start":
                block = event.get("content_block")
                if isinstance(block, Mapping) and block.get("type") == "tool_use":
                    self._record_tool("started", block)
        elif kind == "assistant":
            message = payload.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, Mapping) and block.get("type") == "tool_use":
                        self._record_tool("started", block)
        elif kind == "user":
            message = payload.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, Mapping) and block.get("type") == "tool_result":
                        self._record_tool("completed", block)
        elif kind == "result":
            self._result_payload = dict(payload)

    def _read_stdout(self) -> None:
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            self._stdout_chars += len(line)
            self._last_provider_event = time.monotonic()
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                self._invalid_lines += 1
                continue
            if isinstance(payload, Mapping):
                self._consume(payload)

    def _read_stderr(self) -> None:
        if self.process.stderr is None:
            return
        for chunk in iter(lambda: self.process.stderr.read(4096), ""):
            self._stderr_chars += len(chunk)
            scanned = self._stderr_scan_tail + chunk
            if _is_anthropic_limit(scanned):
                self._limit_seen = True
            if self._failure_kind is None:
                self._failure_kind = _non_retryable_failure(scanned)
            self._stderr_scan_tail = scanned[-4096:]

    def _terminal_from_result(self, returncode: int) -> dict[str, Any]:
        elapsed_ms = int((time.monotonic() - self._started_monotonic) * 1_000)
        telemetry = {
            "duration_ms": elapsed_ms,
            "provider_stdout_chars": self._stdout_chars,
            "provider_stderr_chars": self._stderr_chars,
            "invalid_lines": self._invalid_lines,
            "assistant_chars": self._assistant_chars,
            "tool_started": self._tool_started,
            "tool_completed": self._tool_completed,
            "launch_attempts": self._launch_attempts,
        }
        if self._cancelled:
            return {
                "lifecycle_state": "interrupted",
                "error": "Claude stage was cancelled",
                "telemetry": telemetry,
            }
        if self._timed_out:
            failure_kind = (
                "anthropic_limit"
                if self._limit_seen
                else self._failure_kind or "transient_timeout"
            )
            return {
                "lifecycle_state": "failed",
                "failure_kind": failure_kind,
                "error": "Claude stage timed out",
                "telemetry": telemetry,
            }
        result_is_error = bool(
            isinstance(self._result_payload, Mapping)
            and self._result_payload.get("is_error") is True
        )
        if (
            returncode != 0 and self._limit_seen
        ) or (
            (returncode != 0 or result_is_error)
            and _result_is_anthropic_limit(self._result_payload)
        ):
            return {
                "lifecycle_state": "failed",
                "failure_kind": "anthropic_limit",
                "error": "Anthropic usage limit reached",
                "returncode": returncode,
                "telemetry": telemetry,
            }
        if returncode != 0:
            failure_kind = self._failure_kind or _non_retryable_failure(
                self._result_payload
            )
            if failure_kind is None and (
                _transient_process_failure(self._stderr_scan_tail)
                or _transient_process_failure(self._result_payload)
            ):
                failure_kind = "transient_process_failure"
            if failure_kind is None:
                failure_kind = "process_failure"
            return {
                "lifecycle_state": "failed",
                "failure_kind": failure_kind,
                "error": "Claude Code invocation failed",
                "returncode": returncode,
                "telemetry": telemetry,
            }
        if self._result_payload is None:
            return {
                "lifecycle_state": "failed",
                "failure_kind": "invalid_output",
                "error": "Claude Code returned no valid result",
                "returncode": returncode,
                "telemetry": telemetry,
            }
        if result_is_error:
            failure_kind = self._failure_kind or _non_retryable_failure(
                self._result_payload
            )
            if failure_kind is None and (
                _transient_process_failure(self._stderr_scan_tail)
                or _transient_process_failure(self._result_payload)
            ):
                failure_kind = "transient_process_failure"
            return {
                "lifecycle_state": "failed",
                "failure_kind": failure_kind or "invalid_output",
                "error": "Claude Code returned no valid result",
                "returncode": returncode,
                "telemetry": telemetry,
            }
        payload = self._result_payload
        models = _actual_models(payload)
        quality = _quality_floor(models)
        structured = payload.get("structured_output")
        review_normalization = None
        try:
            if self.profile == "critic":
                result, review_normalization = validate_review_with_normalization(
                    structured, origin="claude"
                )
            else:
                result = validate_implementation_result(structured)
        except InputError as exc:
            return {
                "lifecycle_state": "failed",
                "failure_kind": "invalid_output",
                "error": f"Claude structured output was invalid: {exc}",
                "telemetry": telemetry,
            }
        telemetry.update(
            {
                "requested_model": self.requested_model,
                "actual_models": models,
                "quality_floor_status": quality,
                "requested_effort": "high",
                "actual_effort": _safe_label(payload.get("effort")),
                "num_turns": payload.get("num_turns")
                if isinstance(payload.get("num_turns"), int)
                else None,
                "usage": normalize_usage(payload.get("usage")),
                "provider_duration_ms": payload.get("duration_ms")
                if isinstance(payload.get("duration_ms"), (int, float))
                else None,
            }
        )
        if review_normalization is not None:
            telemetry["review_normalization"] = review_normalization
        if quality == "violated":
            return {
                "lifecycle_state": "failed",
                "failure_kind": "quality_floor",
                "error": "Claude model quality floor was violated",
                "telemetry": telemetry,
            }
        return {
            "lifecycle_state": "completed",
            "result": result,
            "telemetry": telemetry,
        }

    def _wait(self) -> None:
        returncode = self.process.wait()
        for thread in self._threads:
            thread.join(timeout=2)
        for stream in (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        ):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass
        terminal = self._terminal_from_result(returncode)
        with self._lock:
            if self._terminal is not None:
                return
            public_terminal = {
                "stage_id": self.stage_id,
                "profile": self.profile,
                **terminal,
            }
        self._result_payload = None
        self._stderr_scan_tail = ""
        self._emit(
            {
                "type": "stage_terminal",
                "lifecycle_state": public_terminal["lifecycle_state"],
            }
        )
        try:
            self._on_terminal(public_terminal)
        except Exception:
            # The process lifecycle must remain observable even if persistence
            # encounters corrupt state or another unexpected local failure.
            pass
        finally:
            # A callback failure must not leave a finished process looking active.
            with self._lock:
                self._terminal = public_terminal
            self._terminal_ready.set()

    def _watchdog(self) -> None:
        next_heartbeat = time.monotonic() + self._heartbeat
        emitted_stalls = 0
        while self.process.poll() is None:
            time.sleep(min(1.0, self._heartbeat))
            now = time.monotonic()
            if now - self._started_monotonic >= self._timeout:
                self._timed_out = True
                terminate_process(self.process)
                return
            if now >= next_heartbeat:
                idle = int(now - self._last_provider_event)
                self._emit(
                    {
                        "type": "heartbeat",
                        "elapsed_seconds": int(now - self._started_monotonic),
                        "idle_seconds": idle,
                    }
                )
                next_heartbeat = now + self._heartbeat
                stall_count = int(idle // self._stall)
                if stall_count > emitted_stalls:
                    emitted_stalls = stall_count
                    self._emit(
                        {
                            "type": "stalled",
                            "idle_seconds": idle,
                            "stall_count": stall_count,
                        }
                    )

    def poll(self, wait_seconds: float = 0.0) -> dict[str, Any]:
        wait = max(0.0, min(float(wait_seconds), 10.0))
        updates: list[dict[str, Any]] = []
        if wait and self._events.empty() and not self._terminal_ready.is_set():
            try:
                updates.append(self._events.get(timeout=wait))
            except queue.Empty:
                pass
        while len(updates) < 100:
            try:
                updates.append(self._events.get_nowait())
            except queue.Empty:
                break
        with self._lock:
            terminal = dict(self._terminal) if self._terminal else None
        return {
            "stage_id": self.stage_id,
            "profile": self.profile,
            "status": "terminal" if terminal else "running",
            "updates": updates,
            "terminal": terminal,
        }

    def cancel(self) -> dict[str, Any]:
        if self.process.poll() is None:
            self._cancelled = True
            terminate_process(self.process)
            self._terminal_ready.wait(timeout=3)
        return self.poll()
