---
name: workflow
description: Run every repository-mutating coding task through a durable Codex-led contract, deterministic checks, independent review, and persisted completion gate. Use for implementation, fixes, refactors, test changes, configuration changes, and other requests that edit a repository. Do not use for plain questions, explanations, status reports, or read-only diagnosis.
---

# Agent Harness Workflow

Codex remains the user-facing lead and default writer. Apply repository-specific coding or review skills inside this workflow when they match the task.

Before creating a run, classify whether the task is a narrow direct change or needs an approved OpenSpec. A single Jira issue can still require OpenSpec when it changes concurrency, retries, partial-failure behavior, consistency, or several external integrations. In that case switch to `epic-workflow` before `create_run`; do not let a short plan bypass decomposition.

When an `epic-workflow` campaign is active, this skill owns exactly one implementation task. Return its terminal `run_id` to the campaign instead of expanding into sibling tasks.

Before changing files, read [references/protocol.md](references/protocol.md) completely and follow it. Use the `agent-harness` MCP tools to persist the task contract and evidence. The Codex instance that owns the current run, including a campaign executor, owns its state tools. Prefer delegating `start_stage`, `poll_stage`, and `cancel_stage` to one native tracking subagent so model-backed work remains visibly separate. Its task must forbid starting any MCP server or process, including `scripts/mcp_server.py`, and require the exact response `lifecycle tools unavailable` without side effects when those tools are absent. The run owner then calls them through its already configured `agent-harness` MCP server; never launch a second MCP server or process for the same run. In either route Claude remains fresh and independent, and only sanitized progress reaches Codex.

Claude may use the `implement` profile only when the user explicitly requests Claude as the writer for the current task. Otherwise use Claude once, as a fresh read-only `critic`, after deterministic checks pass.

For a direct workflow, when Jira or PR delivery is expected, state in the implementation plan that a fresh user-visible Codex publication task will be created after local completion. The user's approval of that plan explicitly authorizes creating the publication task, but not any external write.

After `finish_run(status: complete)`, a direct-workflow lead hands off only a compact verified summary to that fresh task and uses the `delivery-writing` skill there. A campaign executor instead returns its terminal `run_id` and short summary to the campaign lead; it never creates a publication task itself. The campaign lead creates one immediately for an independently publishable Jira task, without waiting for sibling tasks or campaign completion. Do not fork or copy the implementation conversation. Keep corrections in the implementation context until the current diff is checked, reviewed, and complete; `needs_human`, unresolved review, or stale checks never advance to publication.

Do not treat local completion as permission to push, publish a pull request, change a tracker, deploy, run a migration, or perform another external or irreversible action. Preserve the user's unrelated changes.
