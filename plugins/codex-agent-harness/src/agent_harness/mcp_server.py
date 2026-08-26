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
    "Codex owns epic campaign state, task state, and deterministic checks. "
    "Campaign completion is local and never authorizes tracker or GitHub writes. "
    "Approved OpenSpec references are semantically fingerprinted and rechecked; "
    "local mode requires an ignored project /openspec and keeps a private snapshot. "
    "OpenSpec never replaces campaign execution state. "
    "A replay candidate must be sealed before historical evidence is compared. "
    "Model-backed lifecycle "
    "tools start_stage, poll_stage, and cancel_stage are proxy-only: call them "
    "from one native tracking subagent, not from the user-facing lead. The "
    "critic profile is read-only. The implement profile requires explicit "
    "Claude writer authority in the immutable run contract. A confirmed "
    "Anthropic critic limit permits an explicit Codex fallback review and opens "
    "a campaign cooldown with one later recovery probe. Local completion "
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
CAMPAIGN_ID = {"type": "string", "minLength": 1, "maxLength": 128}
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

CAMPAIGN_SOURCE_SCHEMA = {
    "type": "object",
    "required": ["kind", "ref"],
    "properties": {
        "kind": {"type": "string", "enum": ["jira", "local"]},
        "ref": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    "additionalProperties": False,
}
CAMPAIGN_SPEC_SCHEMA = {
    "type": "object",
    "required": ["kind", "change_id"],
    "properties": {
        "kind": {"type": "string", "enum": ["openspec"]},
        "change_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": "^[a-z0-9][a-z0-9-]*$",
        },
        "storage": {
            "type": "string",
            "enum": ["local", "repository"],
            "default": "local",
            "description": (
                "Local requires ignored project files and stores a private snapshot; "
                "repository explicitly uses versioned openspec/changes/<change-id>."
            ),
        },
    },
    "additionalProperties": False,
}
CAMPAIGN_RUN_SCHEMA = {
    "type": "object",
    "required": ["workspace", "campaign_id", "task_id"],
    "properties": {
        "workspace": WORKSPACE,
        "campaign_id": CAMPAIGN_ID,
        "task_id": {"type": "string", "minLength": 1, "maxLength": 80},
    },
    "additionalProperties": False,
}
CAMPAIGN_TASK_SCHEMA = {
    "type": "object",
    "required": ["id", "title", "goal", "done_when"],
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": 80},
        "title": {"type": "string", "minLength": 1, "maxLength": 300},
        "goal": {"type": "string", "minLength": 1, "maxLength": 12000},
        "done_when": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "kind": {
            "type": "string",
            "enum": ["implementation", "analysis", "delivery"],
        },
        "dependencies": {
            "type": "array",
            "maxItems": 64,
            "items": {"type": "string", "minLength": 1, "maxLength": 80},
        },
        "workspace": WORKSPACE,
        "base_sha": {"type": "string", "minLength": 7, "maxLength": 64},
        "base_from_task": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
        },
        "role": {
            "type": "string",
            "enum": ["task", "integration", "finalizer"],
        },
    },
    "additionalProperties": False,
}
CAMPAIGN_RUBRIC_SCHEMA = {
    "type": "object",
    "required": ["scope", "behavior", "architecture", "tests", "operability"],
    "properties": {
        name: {"type": "integer", "minimum": 0, "maximum": 4}
        for name in ("scope", "behavior", "architecture", "tests", "operability")
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
        "name": "create_campaign",
        "description": (
            "Freeze one local epic campaign with ordered tasks and a server-fingerprinted "
            "OpenSpec snapshot required for high-risk or multi-task delivery. Ignored project "
            "storage is the default; versioned repository storage is explicit. Replay campaigns freeze a "
            "cutoff and withheld-evidence categories. Does not read or change Jira "
            "or GitHub."
        ),
        "inputSchema": {
            "type": "object",
            "required": [
                "workspace",
                "title",
                "goal",
                "done_when",
                "source",
                "tasks",
            ],
            "properties": {
                "workspace": WORKSPACE,
                "title": {"type": "string", "minLength": 1, "maxLength": 300},
                "goal": {"type": "string", "minLength": 1, "maxLength": 12000},
                "done_when": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
                "non_goals": {
                    "type": "array",
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
                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "mode": {"type": "string", "enum": ["delivery", "replay"]},
                "source": CAMPAIGN_SOURCE_SCHEMA,
                "spec": CAMPAIGN_SPEC_SCHEMA,
                "cutoff_at": {"type": "string", "minLength": 1, "maxLength": 64},
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": CAMPAIGN_TASK_SCHEMA,
                },
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Create durable epic campaign", read_only=False, idempotent=False
        ),
    },
    {
        "name": "get_campaign",
        "description": (
            "Read one campaign contract, task progress, intervention summaries, "
            "sealed candidate, and optional replay comparison."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workspace", "campaign_id"],
            "properties": {"workspace": WORKSPACE, "campaign_id": CAMPAIGN_ID},
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Read epic campaign", read_only=True, idempotent=True
        ),
    },
    {
        "name": "list_campaigns",
        "description": "List recent local epic campaigns in this Git checkout.",
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
            "List epic campaigns", read_only=True, idempotent=True
        ),
    },
    {
        "name": "record_campaign_task",
        "description": (
            "Record one ordered campaign task transition. Completing an implementation "
            "task enforces campaign risk, its resolved base, and a terminal complete v1 run."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workspace", "campaign_id", "task_id", "status"],
            "properties": {
                "workspace": WORKSPACE,
                "campaign_id": CAMPAIGN_ID,
                "task_id": {"type": "string", "minLength": 1, "maxLength": 80},
                "status": {
                    "type": "string",
                    "enum": [
                        "in_progress",
                        "complete",
                        "needs_human",
                        "blocked",
                        "failed",
                        "interrupted",
                    ],
                },
                "summary": {"type": "string", "maxLength": 2000},
                "run_workspace": WORKSPACE,
                "run_id": RUN_ID,
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Record epic task progress", read_only=False, idempotent=True
        ),
    },
    {
        "name": "record_campaign_intervention",
        "description": (
            "Record one actual sanitized human context, approval, correction, external "
            "unblock, or blocking question. Operational transitions are counted separately."
        ),
        "inputSchema": {
            "type": "object",
            "required": [
                "workspace",
                "campaign_id",
                "intervention_id",
                "kind",
                "reason",
                "blocking",
                "resolved",
            ],
            "properties": {
                "workspace": WORKSPACE,
                "campaign_id": CAMPAIGN_ID,
                "intervention_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 80,
                },
                "kind": {
                    "type": "string",
                    "enum": [
                        "blocking_question",
                        "approval",
                        "correction",
                        "context",
                        "external_unblock",
                    ],
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                "outcome": {"type": "string", "maxLength": 2000},
                "blocking": {"type": "boolean"},
                "resolved": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Record human intervention", read_only=False, idempotent=True
        ),
    },
    {
        "name": "seal_campaign_candidate",
        "description": (
            "Freeze the campaign's own result after every task completed and every "
            "blocking intervention was resolved. Historical replay evidence remains "
            "out of scope until this succeeds."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workspace", "campaign_id", "summary"],
            "properties": {
                "workspace": WORKSPACE,
                "campaign_id": CAMPAIGN_ID,
                "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Seal epic candidate", read_only=False, idempotent=True
        ),
    },
    {
        "name": "record_campaign_comparison",
        "description": (
            "After a replay candidate is sealed, record cutoff fidelity, historical "
            "similarity, gap attribution, and readiness. Raw diffs and connector "
            "output must not be supplied."
        ),
        "inputSchema": {
            "type": "object",
            "required": [
                "workspace",
                "campaign_id",
                "rubric",
                "cutoff_rubric",
                "candidate_readiness",
            ],
            "properties": {
                "workspace": WORKSPACE,
                "campaign_id": CAMPAIGN_ID,
                "rubric": CAMPAIGN_RUBRIC_SCHEMA,
                "cutoff_rubric": CAMPAIGN_RUBRIC_SCHEMA,
                "candidate_readiness": {
                    "type": "string",
                    "enum": ["unsafe", "partial", "ready"],
                },
                "gap_attribution": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "required": ["gap", "category"],
                        "properties": {
                            "gap": {"type": "string", "maxLength": 2000},
                            "category": {
                                "type": "string",
                                "enum": [
                                    "derivable_miss",
                                    "underspecified",
                                    "historical_only",
                                    "intentional_alternative",
                                ],
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "similarities": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {"type": "string", "maxLength": 2000},
                },
                "differences": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {"type": "string", "maxLength": 2000},
                },
                "residual_risks": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {"type": "string", "maxLength": 2000},
                },
                "historical_refs": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {"type": "string", "maxLength": 1000},
                },
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Record replay comparison", read_only=False, idempotent=True
        ),
    },
    {
        "name": "finish_campaign",
        "description": (
            "Finish a local epic campaign. Complete requires a sealed candidate and, "
            "for replay, a historical comparison. It never authorizes external actions."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["workspace", "campaign_id", "status"],
            "properties": {
                "workspace": WORKSPACE,
                "campaign_id": CAMPAIGN_ID,
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
            },
            "additionalProperties": False,
        },
        "annotations": _annotations(
            "Finish epic campaign", read_only=False, idempotent=True
        ),
    },
    {
        "name": "create_run",
        "description": (
            "Create one immutable task contract in target Git metadata before "
            "repository edits. An optional campaign link resolves base and minimum risk. "
            "Rejects dirty worktrees unless explicitly acknowledged."
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
                "base_sha": {
                    "type": "string",
                    "minLength": 7,
                    "maxLength": 64,
                },
                "campaign": CAMPAIGN_RUN_SCHEMA,
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
            "gates are enforced before inference; a campaign cooldown may skip Claude "
            "and enable the explicit Codex fallback. Never call from the Codex lead."
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
            "Record Codex verification of critic findings. Also accepts an independent "
            "Codex review when Claude wrote the change, or one fresh Codex fallback "
            "review after a confirmed Anthropic critic limit."
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
            "create_campaign": self.service.create_campaign,
            "get_campaign": self.service.get_campaign,
            "list_campaigns": self.service.list_campaigns,
            "record_campaign_task": self.service.record_campaign_task,
            "record_campaign_intervention": self.service.record_campaign_intervention,
            "seal_campaign_candidate": self.service.seal_campaign_candidate,
            "record_campaign_comparison": self.service.record_campaign_comparison,
            "finish_campaign": self.service.finish_campaign,
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
