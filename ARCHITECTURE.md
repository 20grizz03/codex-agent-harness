# Architecture

## Boundaries

Codex owns the user conversation, repository mutations, project-command execution, finding triage, and the completion decision. The MCP server owns immutable contracts, durable state, check planning, evidence validation, and Claude process lifecycle. Claude receives one autonomous stage and never commits, pushes, publishes, deploys, or mutates external systems.

The MCP server does not execute project checks. It returns argv arrays derived from the immutable contract and `.codex/agent-harness.json`; Codex executes them with its existing sandbox and egress controls, then records bounded outcomes.

## State

Each run is stored under the absolute Git directory of its worktree. `contract.json` is created once. `state.json` is atomically replaced. `events.jsonl` is append-only. `review.json` contains only the validated structured review and Codex dispositions. Files are mode `0600`; directories are mode `0700`.

The lifecycle is `prepared → writing → checking → reviewing → correcting → checking → complete`, with `needs_human`, `blocked`, `failed`, and `interrupted` as terminal alternatives. A server restart converts an in-flight model stage to `interrupted`; inference is never automatically repeated after it may have started.

## Review

The default stage is a read-only Claude Opus critic using a fresh context and repository-native Git inspection. It receives the task contract and check evidence, not a pasted diff. Claude returns a strict JSON result. Codex verifies each finding and may perform one correction pass; checks are invalidated whenever the diff fingerprint changes. Resolutions from that review may be updated after the corrected diff passes its own checks without launching a second critic.

If the Claude process reports a recognized Anthropic usage-limit code or message, the failed stage stores only `failure_kind: anthropic_limit`. The run stays in `reviewing` and may accept one fresh read-only native Codex review with origin `codex_fallback`. Raw provider text is discarded. Timeouts, authentication failures, process errors, invalid structured output, cancellation, and quality-floor violations do not enable this path.

Claude can be the writer only when the immutable contract records explicit user intent. In that mode Codex supplies the independent structured review, so the same provider never acts as both writer and critic.
