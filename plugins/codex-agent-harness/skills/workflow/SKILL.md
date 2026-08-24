---
name: workflow
description: Run every repository-mutating coding task through a durable Codex-led contract, deterministic checks, independent review, and persisted completion gate. Use for implementation, fixes, refactors, test changes, configuration changes, and other requests that edit a repository. Do not use for plain questions, explanations, status reports, or read-only diagnosis.
---

# Agent Harness Workflow

Codex remains the user-facing lead and default writer. Apply repository-specific coding or review skills inside this workflow when they match the task.

When an `epic-workflow` campaign is active, this skill owns exactly one implementation task. Return its terminal `run_id` to the campaign instead of expanding into sibling tasks.

Before changing files, read [references/protocol.md](references/protocol.md) completely and follow it. Use the `agent-harness` MCP tools to persist the task contract and evidence. The Codex lead owns state tools; delegate `start_stage`, `poll_stage`, and `cancel_stage` to one native tracking subagent so model-backed work remains visibly separate.

Claude may use the `implement` profile only when the user explicitly requests Claude as the writer for the current task. Otherwise use Claude once, as a fresh read-only `critic`, after deterministic checks pass.

After implementation and current-diff checks are complete, use the `delivery-writing` skill when the task needs Jira wording, testing recommendations, a PR description, or colleague-facing review comments. Those artifacts are derived from the verified implementation and do not grant permission to publish them.

Do not treat local completion as permission to push, publish a pull request, change a tracker, deploy, run a migration, or perform another external or irreversible action. Preserve the user's unrelated changes.
