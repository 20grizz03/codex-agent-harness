"""Strict structured contracts for Claude criticism and implementation."""

from __future__ import annotations

import json
from typing import Any

from .util import (
    FINDING_ID_RE,
    InputError,
    numeric_tree,
    require_string,
    require_string_list,
    sanitize_string_list,
    sanitize_text,
)


SEVERITIES = ("P0", "P1", "P2", "P3")
VERDICTS = ("pass", "changes_requested", "blocked")

REVIEW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "verdict",
        "findings",
        "residual_risks",
        "blocking_question",
    ],
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "findings": {
            "type": "array",
            "maxItems": 64,
            "items": {
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
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "file": {"type": "string", "maxLength": 1_000},
                    "line": {"type": ["integer", "null"], "minimum": 1},
                    "title": {"type": "string", "maxLength": 500},
                    "impact": {"type": "string", "maxLength": 2_000},
                    "evidence": {"type": "string", "maxLength": 4_000},
                    "fix": {"type": "string", "maxLength": 2_000},
                },
                "additionalProperties": False,
            },
        },
        "residual_risks": {
            "type": "array",
            "maxItems": 64,
            "items": {"type": "string", "maxLength": 1_000},
        },
        "blocking_question": {
            "type": ["string", "null"],
            "maxLength": 2_000,
        },
    },
    "additionalProperties": False,
}

IMPLEMENT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "summary",
        "changed_paths",
        "checks_run",
        "risks",
        "blockers",
    ],
    "properties": {
        "summary": {"type": "string", "maxLength": 4_000},
        "changed_paths": {
            "type": "array",
            "maxItems": 128,
            "items": {"type": "string", "maxLength": 1_000},
        },
        "checks_run": {
            "type": "array",
            "maxItems": 64,
            "items": {"type": "string", "maxLength": 1_000},
        },
        "risks": {
            "type": "array",
            "maxItems": 64,
            "items": {"type": "string", "maxLength": 1_000},
        },
        "blockers": {
            "type": "array",
            "maxItems": 16,
            "items": {"type": "string", "maxLength": 1_000},
        },
    },
    "additionalProperties": False,
}


def validate_review(value: Any, *, origin: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError("review must be a JSON object")
    verdict = value.get("verdict")
    if verdict not in VERDICTS:
        raise InputError("review.verdict is invalid")
    raw_findings = value.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > 64:
        raise InputError("review.findings must be an array with at most 64 items")
    findings: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_findings):
        if not isinstance(raw, dict):
            raise InputError(f"review.findings[{index}] must be an object")
        finding_id = require_string(raw.get("id"), "finding.id", maximum=80)
        if not FINDING_ID_RE.fullmatch(finding_id):
            raise InputError("finding.id contains unsupported characters")
        if finding_id in seen_ids:
            raise InputError(f"duplicate finding id: {finding_id}")
        seen_ids.add(finding_id)
        severity = raw.get("severity")
        if severity not in SEVERITIES:
            raise InputError("finding.severity must be P0, P1, P2, or P3")
        line = raw.get("line")
        if line is not None and (
            not isinstance(line, int) or isinstance(line, bool) or line < 1
        ):
            raise InputError("finding.line must be a positive integer or null")
        findings.append(
            {
                "id": finding_id,
                "severity": severity,
                "file": require_string(raw.get("file"), "finding.file", maximum=1_000),
                "line": line,
                "title": sanitize_text(
                    require_string(raw.get("title"), "finding.title", maximum=500),
                    maximum=500,
                ),
                "impact": sanitize_text(
                    require_string(raw.get("impact"), "finding.impact", maximum=2_000),
                    maximum=2_000,
                ),
                "evidence": sanitize_text(
                    require_string(raw.get("evidence"), "finding.evidence", maximum=4_000),
                    maximum=4_000,
                ),
                "fix": sanitize_text(
                    require_string(raw.get("fix"), "finding.fix", maximum=2_000),
                    maximum=2_000,
                ),
            }
        )
    risks = sanitize_string_list(value.get("residual_risks"))
    question = value.get("blocking_question")
    if question is not None:
        question = sanitize_text(
            require_string(question, "review.blocking_question", maximum=2_000),
            maximum=2_000,
        )
    if verdict == "pass" and findings:
        raise InputError("a pass review must not contain findings")
    if verdict == "changes_requested" and not findings:
        raise InputError("a changes_requested review must contain a finding")
    if verdict == "blocked" and not question:
        raise InputError("a blocked review requires one blocking_question")
    return {
        "origin": origin,
        "verdict": verdict,
        "findings": findings,
        "residual_risks": risks,
        "blocking_question": question,
    }


def validate_implementation_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError("implementation result must be a JSON object")
    return {
        "summary": sanitize_text(
            require_string(value.get("summary"), "implementation.summary", maximum=4_000),
            maximum=4_000,
        ),
        "changed_paths": require_string_list(
            value.get("changed_paths"),
            "implementation.changed_paths",
            maximum_items=128,
        ),
        "checks_run": require_string_list(
            value.get("checks_run"), "implementation.checks_run"
        ),
        "risks": require_string_list(value.get("risks"), "implementation.risks"),
        "blockers": require_string_list(
            value.get("blockers"),
            "implementation.blockers",
            maximum_items=16,
        ),
    }


COMMON_SYSTEM_PROMPT = """
You are an autonomous Claude Code stage inside Codex Agent Harness. Codex is
the user-facing lead and owns the final decision. Work only inside the supplied
repository. Read and obey applicable AGENTS.md and CLAUDE.md files. Never copy
credential values into output or query external secret stores. Do not commit,
push, publish, create a pull request, change a tracker, deploy, migrate, contact
external services, or mutate external accounts. Preserve unrelated changes.
Before running any command that might contact queues, mail, databases, caches,
webhooks, analytics, storage, or external APIs, inspect configuration and do
not run it unless the target is demonstrably local or fake. Respond in the
strict JSON shape requested by the CLI. Use Russian prose unless the task asks
for another language; preserve code and identifiers exactly.
""".strip()

CRITIC_SYSTEM_PROMPT = (
    COMMON_SYSTEM_PROMPT
    + "\n\n"
    + """
Act as a fresh independent critic. You are read-only. Inspect the repository,
Git diff, surrounding production paths, tests, and relevant history yourself.
Do not edit files. Report only actionable correctness, security, reliability,
or contract defects with concrete impact and evidence. Do not repeat a known
failed check as a new finding. Put uncertain validation gaps in residual_risks.
""".strip()
)

IMPLEMENT_SYSTEM_PROMPT = (
    COMMON_SYSTEM_PROMPT
    + "\n\n"
    + """
The user explicitly selected Claude as the writer for this run. Orient in the
repository, implement the complete contract, preserve unrelated work, and run
only focused checks that are safe under the egress rules. Do not leave stubs.
Codex will independently inspect and validate the result afterward.
""".strip()
)


def build_stage_prompt(
    *,
    profile: str,
    contract: dict[str, Any],
    state: dict[str, Any],
) -> str:
    check_results = state.get("check_results", {}).get(
        state.get("diff_fingerprint", ""), {}
    )
    packet = {
        "task_contract": {
            key: contract.get(key)
            for key in (
                "run_id",
                "repo_root",
                "base_sha",
                "goal",
                "non_goals",
                "done_when",
                "constraints",
                "forbidden_actions",
                "writer",
                "risk",
            )
        },
        "current_evidence": {
            "diff_fingerprint": state.get("diff_fingerprint"),
            "changed_paths": state.get("changed_paths", []),
            "risk": state.get("risk"),
            "checks": [
                {
                    "name": name,
                    "status": result.get("status"),
                    "exit_code": result.get("exit_code"),
                    "duration_ms": result.get("duration_ms"),
                    "summary": result.get("summary", ""),
                }
                for name, result in sorted(check_results.items())
            ],
        },
    }
    instruction = (
        "Inspect this repository and review its current changes against the packet."
        if profile == "critic"
        else "Implement the immutable task contract in this repository."
    )
    return (
        f"{instruction}\n\n"
        "The packet contains identifiers and evidence, not the Git diff. Read Git directly.\n"
        f"<agent_harness_packet>\n{json.dumps(packet, ensure_ascii=False, indent=2)}\n"
        "</agent_harness_packet>"
    )


def normalize_usage(value: Any) -> dict[str, Any]:
    normalized = numeric_tree(value)
    return normalized if isinstance(normalized, dict) else {}
