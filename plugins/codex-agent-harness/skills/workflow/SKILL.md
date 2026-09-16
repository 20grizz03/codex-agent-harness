---
name: workflow
description: Run repository-changing implementation through a durable Codex-led contract, deterministic checks, independent review, and persisted completion gate. Use for code, tests, behavior, configuration, security, instructions, and other material repository changes. Obvious low-risk non-executable text edits may use its concise direct path.
---

# Agent Harness Workflow

Codex remains the user-facing lead and default writer. Apply repository-specific coding or review instructions when they match the task.

Use the writing mode of [delivery-writing](../delivery-writing/SKILL.md) before the first human-facing draft, including the plan, code comments, documentation, errors and readable logs. Do not wait for `finish_run` or publication approval. Writing rules do not start a publication package or replace this task's checks.

Before creating a run, classify the change as a concise low-risk edit, a small publication follow-up, a mechanical dependency adaptation, a standard direct run, or a specification-backed campaign. In an active publication context, first consider the [small follow-up route](../delivery-writing/references/publication-context.md#мелкие-доработки): it permits bounded code fixes without a new run, mandatory tests, or independent review, and explicitly records unverified changes. If that candidate has dependent branches, read the [correction chain](references/correction-chain.md) before updating them. Mechanical adaptation of already approved behavior does not become a full run merely because it touches a dependent PR; a semantic change receives a new scoped run. A single Jira issue can still need an approved specification when it changes concurrency, retries, partial-failure behavior, consistency, security, compatibility, or several external integrations. Native Markdown is the default; OpenSpec remains available when explicitly selected or already frozen. File count and diff size do not decide the route.

For any correction mode, give a short progress report after 15 minutes of active work, separating writing, propagation, checks, review, and external waiting. External waiting does not count toward the active-work threshold; this is not a pause or a new approval gate.

For the selected route, read [testing within the task](references/testing.md) before choosing checks or writing tests. Apply its relevant guidance inside the existing plan, implementation and verification stages; it does not add a QA cycle or override the small publication follow-up exception.

When an `epic-workflow` campaign is active, this skill owns exactly one implementation task. Return its terminal `run_id` to the campaign instead of expanding into sibling tasks.

Before changing files, read [references/protocol.md](references/protocol.md) completely and follow it. Standard runs persist the task contract and evidence with the `agent-harness` MCP tools. The Codex instance that owns the run owns its state tools. Prefer delegating `start_stage`, `poll_stage`, and `cancel_stage` to one native tracking subagent; never launch a second MCP server or process. Claude remains fresh and independent, and only sanitized progress reaches Codex.

Prepared campaign tasks default to native `gpt-5.6-sol` execution at `high` reasoning effort. The campaign lead passes both stored values explicitly when spawning the executor; the MCP server records this planning contract but does not attest which native model ran. Claude may use `implement` only when the user explicitly requests it. Otherwise Claude is the fresh read-only critic.

For a reviewed but unfinished candidate, read [review follow-ups](references/review-followups.md) before choosing another full cycle. It defines scoped correction review and a server-validated nonsemantic P3 closeout that retains the original review provenance while rerunning mandatory checks.

Once the user approves a concrete plan, continue its reversible local edits, checks, corrections, authorized commits, and unchanged retries without asking again across turns. Resolve a reversible non-semantic overlap when intent is clear and the user authorized it. Ask only when product behavior or scope changes, user-owned intent cannot be determined safely, or an external or irreversible action has not already been authorized.

After `finish_run(status: complete)`, use the publication mode of `delivery-writing` in the current user-visible task and verify every candidate before showing a publication package. A campaign executor returns its terminal `run_id` and short summary to the campaign lead. Create a separate fresh task only when the user explicitly selected that boundary; publication itself never requires it. Stale checks, stale review, or unresolved findings remain in implementation.

Local completion does not authorize push, pull-request or tracker changes, deployment, migration, or another external or irreversible action. Preserve unrelated user changes.
