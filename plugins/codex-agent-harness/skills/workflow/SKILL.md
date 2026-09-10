---
name: workflow
description: Run repository-changing implementation through a durable Codex-led contract, deterministic checks, independent review, and persisted completion gate. Use for code, tests, behavior, configuration, security, instructions, and other material repository changes. Obvious low-risk non-executable text edits may use its concise direct path.
---

# Agent Harness Workflow

Codex remains the user-facing lead and default writer. Apply repository-specific coding or review instructions when they match the task.

Before creating a run, classify the change as a concise low-risk edit, a small publication follow-up, a standard direct run, or an OpenSpec campaign. In an active publication context, first consider the [small follow-up route](../delivery-writing/references/publication-context.md#мелкие-доработки): it permits bounded code fixes without a new run, mandatory tests, or independent review, and explicitly records unverified changes. A single Jira issue can still require OpenSpec when it changes concurrency, retries, partial-failure behavior, consistency, security, compatibility, or several external integrations. File count and diff size do not decide the route.

When an `epic-workflow` campaign is active, this skill owns exactly one implementation task. Return its terminal `run_id` to the campaign instead of expanding into sibling tasks.

Before changing files, read [references/protocol.md](references/protocol.md) completely and follow it. Standard runs persist the task contract and evidence with the `agent-harness` MCP tools. The Codex instance that owns the run owns its state tools. Prefer delegating `start_stage`, `poll_stage`, and `cancel_stage` to one native tracking subagent; never launch a second MCP server or process. Claude remains fresh and independent, and only sanitized progress reaches Codex.

Prepared campaign tasks default to native `gpt-5.6-sol` execution at `high` reasoning effort. The campaign lead passes both stored values explicitly when spawning the executor; the MCP server records this planning contract but does not attest which native model ran. Claude may use `implement` only when the user explicitly requests it. Otherwise Claude is the fresh read-only critic.

For an already reviewed candidate, read [review follow-ups](references/review-followups.md) before choosing another full cycle. It defines scoped correction review and a server-validated nonsemantic P3 closeout that retains the original review provenance while rerunning mandatory checks.

Once the user approves a concrete plan, continue its reversible local edits, checks, corrections, authorized commits, and unchanged retries without asking again across turns. Resolve a reversible non-semantic overlap when intent is clear and the user authorized it. Ask only when product behavior or scope changes, user-owned intent cannot be determined safely, or an external or irreversible action has not already been authorized.

After `finish_run(status: complete)`, use `delivery-writing` in the current user-visible task and verify every candidate before showing a publication package. A campaign executor returns its terminal `run_id` and short summary to the campaign lead. Create a separate fresh task only when the user explicitly selected that boundary; publication itself never requires it. Stale checks, stale review, or unresolved findings remain in implementation.

Local completion does not authorize push, pull-request or tracker changes, deployment, migration, or another external or irreversible action. Preserve unrelated user changes.
