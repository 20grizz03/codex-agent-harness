"""Minimal stdio MCP server exposing the Agent Harness service."""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Mapping

from . import __version__
from .service import HarnessService
from .util import HarnessError, InputError, require_string


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "agent-harness"
SERVER_INSTRUCTIONS = (
    "Codex owns task state and deterministic checks. Model-backed lifecycle "
    "tools start_stage, poll_stage, and cancel_stage are proxy-only: call them "
    "from one native tracking subagent, not from the user-facing lead. The "
    "critic profile is read-only. The implement profile requires explicit "
    "Claude writer authority in the immutable run contract. Local completion "
    "does not authorize push, PR publication, tracker changes, deploys, or "
    "other external mutations."
)


def _tool_result(payload: Mapping[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    safe = dict(payload)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(safe, ensure_ascii=False, sort_keys=True),
            }
        ],
        "structuredContent": safe,
        "isError": is_error,
    }


WORKSPACE = {"type": "string", "minLength": 1, "maxLength": 4096}
RUN_ID = {"type": "string", "minLength": 1, "maxLength": 128}
CHECK_SCHEMA = {
    "type": "object",
    "required": ["name", "argv"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 80},
        "argv": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": 14400,
        },
    },
    "additionalProperties": False,
}
FINDING_SCHEMA = {
    "type": "object",
    "required": [
        "id",
        "severity",
        "file",
        "line",
        "title",
        "impact",
        "evidence",
        "fix",
    ],
    "properties": {
        "id": {"type": "string", "maxLength": 80},
        "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
        "file": {"type": "string", "maxLength": 1000},
        "line": {"type": ["integer", "null"], "minimum": 1},
        "title": {"type": "string", "maxLength": 500},
        "impact": {"type": "string", "maxLength": 2000},
        "evidence": {"type": "string", "maxLength": 4000},
        "fix": {"type": "string", "maxLength": 2000},
    },
    "additionalProperties": False,
}
REVIEW_SCHEMA = {
    "type": "object",
    "required": [
        "verdict",
        "findings",
        "residual_risks",
        "blocking_question",
    ],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["pass", "changes_requested", "blocked"],
        },
        "findings": {
            "type": "array",
            "maxItems": 64,
            "items": FINDING_SCHEMA,
        },
        "residual_risks": {
            "type": "array",
            "maxItems": 64,
            "items": {"type": "string", "maxLength": 1000},
        },
        "blocking_question": {
            "type": ["string", "null"],
            "maxLength": 2000,
        },
    },
    "additionalProperties": False,
}


def _annotations(title: str, *, read_only: bool, idempotent: bool) -> dict[str, Any]:
    return {
        "title": title,
        "readOnlyHint": read_only,
        "destructiveHint": False,
        "idempotentHint": idempotent,
        "openWorldHint": False,
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "check_runtime",
        "description": (
            "Check Claude Code CLI, public auth fields, required safety flags, "
            "Opus policy, and API/provider-billing guards. Never invokes a model."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Check Agent Harness runtime", read_only=True, idempotent=True
        ),
    },
    {
        "name": "create_run",
        "description": (
            "Create one immutable task contract in target Git metadata before "
            "repository edits. Rejects dirty worktrees unless explicitly acknowledged."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workspace", "goal", "done_when"],
            "properties": {
                "workspace": WORKSPACE,
                "goal": {"type": "string", "minLength": 1, "maxLength": 12000},
                "non_goals": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {"type": "string", "maxLength": 1000},
                },
                "done_when": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": {"type": "string", "maxLength": 1000},
                },
                "constraints": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {"type": "string", "maxLength": 1000},
                },
                "forbidden_actions": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {"type": "string", "maxLength": 1000},
                },
                "writer": {"type": "string", "enum": ["codex", "claude"]},
                "writer_explicit": {"type": "boolean"},
                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "required_checks": {
                    "type": "array",
                    "maxItems": 64,
                    "items": CHECK_SCHEMA,
                },
                "max_correction_passes": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 1,
                },
                "allow_dirty": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Create durable task run", read_only=False, idempotent=False
        ),
    },
    {
        "name": "get_run",
        "description": (
            "Read one durable task contract, lifecycle state, check evidence, "
            "review, and finding resolutions. Recovers stale inference as interrupted."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workspace", "run_id"],
            "properties": {"workspace": WORKSPACE, "run_id": RUN_ID},
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Read durable task run", read_only=True, idempotent=True
        ),
    },
    {
        "name": "list_runs",
        "description": "List recent Agent Harness runs stored in this worktree's Git metadata.",
        "inputSchema": {
            "type": "object",
            "required": ["workspace"],
            "properties": {
                "workspace": WORKSPACE,
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "List durable task runs", read_only=True, idempotent=True
        ),
    },
    {
        "name": "plan_checks",
        "description": (
            "Fingerprint the current diff and return the union of the always-on "
            "gate, frozen repository checks, and matching path policy. Does not execute commands."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workspace", "run_id"],
            "properties": {"workspace": WORKSPACE, "run_id": RUN_ID},
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Plan checks for current diff", read_only=False, idempotent=True
        ),
    },
    {
        "name": "record_check",
        "description": (
            "Record one bounded project-check result for the currently planned "
            "diff fingerprint. Raw command output and secrets must not be supplied."
        ),
        "inputSchema": {
            "type": "object",
            "required": [
                "workspace",
                "run_id",
                "check_name",
                "exit_code",
                "duration_ms",
            ],
            "properties": {
                "workspace": WORKSPACE,
                "run_id": RUN_ID,
                "check_name": {"type": "string", "maxLength": 80},
                "exit_code": {"type": "integer"},
                "duration_ms": {"type": "integer", "minimum": 0},
                "summary": {"type": "string", "maxLength": 2000},
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Record check evidence", read_only=False, idempotent=True
        ),
    },
    {
        "name": "start_stage",
        "description": (
            "PROXY-ONLY: start the single Claude critic or explicitly authorized "
            "Claude implementation stage. Subscription readiness and green-check "
            "gates are enforced before inference. Never call from the Codex lead."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workspace", "run_id", "profile"],
            "properties": {
                "workspace": WORKSPACE,
                "run_id": RUN_ID,
                "profile": {"type": "string", "enum": ["critic", "implement"]},
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Start tracked Claude stage", read_only=False, idempotent=True
        ),
    },
    {
        "name": "poll_stage",
        "description": (
            "PROXY-ONLY: read allowlisted progress and terminal metadata for one "
            "Claude stage. Raw JSONL, prompts, tool arguments, and stderr are discarded."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workspace", "run_id", "stage_id"],
            "properties": {
                "workspace": WORKSPACE,
                "run_id": RUN_ID,
                "stage_id": {"type": "string", "maxLength": 128},
                "wait_seconds": {"type": "number", "minimum": 0, "maximum": 10},
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Poll tracked Claude stage", read_only=True, idempotent=True
        ),
    },
    {
        "name": "cancel_stage",
        "description": (
            "PROXY-ONLY: cancel one active Claude stage without retrying or "
            "starting a replacement. Never call from the Codex lead."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workspace", "run_id", "stage_id"],
            "properties": {
                "workspace": WORKSPACE,
                "run_id": RUN_ID,
                "stage_id": {"type": "string", "maxLength": 128},
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Cancel tracked Claude stage", read_only=False, idempotent=True
        ),
    },
    {
        "name": "record_review_resolution",
        "description": (
            "Record Codex verification of critic findings. For an explicitly "
            "Claude-written run, also accepts the independent Codex structured review."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workspace", "run_id"],
            "properties": {
                "workspace": WORKSPACE,
                "run_id": RUN_ID,
                "review": REVIEW_SCHEMA,
                "resolutions": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "required": [
                            "finding_id",
                            "disposition",
                            "resolved",
                            "evidence",
                        ],
                        "properties": {
                            "finding_id": {"type": "string", "maxLength": 80},
                            "disposition": {
                                "type": "string",
                                "enum": ["accepted", "rejected", "unverified"],
                            },
                            "resolved": {"type": "boolean"},
                            "evidence": {"type": "string", "maxLength": 4000},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Record review dispositions", read_only=False, idempotent=True
        ),
    },
    {
        "name": "finish_run",
        "description": (
            "Finish a run. complete is accepted only for the current checked "
            "diff with an independent review, resolved findings, and no blocker."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workspace", "run_id", "status"],
            "properties": {
                "workspace": WORKSPACE,
                "run_id": RUN_ID,
                "status": {
                    "type": "string",
                    "enum": [
                        "complete",
                        "needs_human",
                        "blocked",
                        "failed",
                        "interrupted",
                    ],
                },
                "summary": {"type": "string", "maxLength": 2000},
                "blocking_question": {"type": "string", "maxLength": 2000},
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Finish durable task run", read_only=False, idempotent=True
        ),
    },
]


class McpServer:
    def __init__(self, service: HarnessService | None = None) -> None:
        self.service = service or HarnessService()
        self._handlers: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            "check_runtime": self.service.check_runtime,
            "create_run": self.service.create_run,
            "get_run": self.service.get_run,
            "list_runs": self.service.list_runs,
            "plan_checks": self.service.plan_checks,
            "record_check": self.service.record_check,
            "start_stage": self.service.start_stage,
            "poll_stage": self.service.poll_stage,
            "cancel_stage": self.service.cancel_stage,
            "record_review_resolution": self.service.record_review_resolution,
            "finish_run": self.service.finish_run,
        }

    def call_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            return _tool_result(
                {"error": "tool arguments must be an object"}, is_error=True
            )
        handler = self._handlers.get(name)
        if handler is None:
            return _tool_result({"error": f"unknown tool: {name}"}, is_error=True)
        try:
            return _tool_result(handler(arguments))
        except (HarnessError, OSError, ValueError) as exc:
            return _tool_result({"error": str(exc)}, is_error=True)

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        if request_id is None:
            return None
        try:
            if method == "initialize":
                result: dict[str, Any] = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": __version__},
                    "instructions": SERVER_INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = request.get("params")
                if not isinstance(params, Mapping):
                    raise InputError("tools/call params must be an object")
                name = require_string(params.get("name"), "tool name", maximum=80)
                result = self.call_tool(name, params.get("arguments", {}))
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"method not found: {method}",
                    },
                }
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except HarnessError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": str(exc)},
            }


def serve() -> int:
    server = McpServer()
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise ValueError("request must be an object")
            response = server.handle(request)
        except (json.JSONDecodeError, ValueError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(exc)},
            }
        if response is not None:
            sys.stdout.write(
                json.dumps(response, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
