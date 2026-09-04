# Architecture

## Boundaries

Codex owns the user conversation, native executor creation, repository mutations, project-command execution, finding triage, and the completion decision. A prepared campaign task records `execution.native_model`, `execution.reasoning_effort`, and an optional `execution.escalation_model`; the lead passes the first two values explicitly to the native executor. Runtime defaults are `gpt-5.6-sol`, `high`, and a null escalation model; an epic plan may explicitly name `gpt-6-astra` for planning or recorded escalation. The MCP server owns immutable contracts, durable state, check planning, evidence validation, and Claude process lifecycle, but never launches or attests Sol. Claude never commits, pushes, publishes, deploys, or mutates external systems.

The MCP server does not execute project checks. It returns argv arrays derived from the immutable contract and `.codex/agent-harness.json`; Codex executes them with its existing sandbox and egress controls, then records bounded outcomes.

## State

Each run is stored under the absolute Git directory of its worktree. `contract.json` is created once. A campaign-linked run inherits the task goal, completion criteria, constraints, non-goals, forbidden actions, required checks, versioned contract references, execution settings, and bounded review budgets. Callers may add criteria, restrictions, checks, and references, but cannot override a same-name check or frozen setting. `state.json` is atomically replaced and tracks `review_cycle` and `critic_retries`. `events.jsonl` is append-only. `review.json` keeps the current review with `cycle`, `diff_fingerprint`, and Claude `stage_id`, plus prior cycles in `history` and Codex dispositions. Files are mode `0600`; directories are mode `0700`.

The lifecycle is `prepared → writing → checking → reviewing → correcting → checking → reviewing → complete`, with bounded repetitions of correction, checking, and review. `needs_human`, `blocked`, `failed`, and `interrupted` remain terminal alternatives. A server restart converts an in-flight model stage to `interrupted`; inference is never automatically repeated after it may have started.

## Review

The default review stage is a read-only Claude Opus critic using a fresh context and repository-native Git inspection. It receives the task contract and check evidence, not a pasted diff. Claude returns a strict JSON result. Codex verifies each finding. Checks and the current review are invalidated whenever the full-result fingerprint changes; a corrected result must pass checks and receive a new independent review within the frozen correction budget. Prior cycles remain in history.

If a stage ends as `transient_timeout` or `transient_process_failure`, an explicit `start_stage.retry_stage_id` may name that failed stage and create one new critic stage while `max_critic_retries` remains. The new stage records `retry_of`; repeating the same request is idempotent. Authentication, billing, safety, cancellation, invalid structured output, and model-policy failures are never retryable.

If the Claude process reports a recognized Anthropic usage-limit code or message, the failed stage stores only `failure_kind: anthropic_limit`. The run stays in `reviewing` and may accept one fresh read-only native Codex review with origin `codex_fallback`. Raw provider text is discarded. This fallback remains distinct from transient retry and all other failures.

Claude can be the writer only when the immutable contract records explicit user intent. In that mode Codex supplies the independent structured review, so the same provider never acts as both writer and critic.
