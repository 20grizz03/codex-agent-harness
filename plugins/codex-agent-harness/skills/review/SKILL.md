---
name: review
description: Independently review a repository result with a fresh read-only Claude critic, then have Codex verify every finding; permits one policy-bounded transient retry and one explicitly marked Codex fallback only for a confirmed Anthropic limit. Use for an independent review, Claude second opinion, adversarial bug hunt, or Agent Harness review.
---

# Agent Harness Review

Codex owns the review target, final judgment, and user-facing report. Read [references/review-contract.md](references/review-contract.md) completely before invoking the critic.

Create a run if the review is not already part of an active workflow. Record applicable deterministic checks first. Prefer delegating the model lifecycle to one native tracking subagent. Its task must forbid starting any MCP server or process, including `scripts/mcp_server.py`, and require `lifecycle tools unavailable` without side effects when plugin lifecycle tools are absent. The Codex instance that owns the run then calls them through its already configured `agent-harness` MCP server; never start another MCP server or process for the same run. Claude must use the read-only `critic` profile and inspect the repository itself; Codex receives only sanitized progress and the structured result.

If the persisted Claude stage reports `transient_timeout` or `transient_process_failure`, one explicit retry may name the failed stage only when the immutable run budget allows it. Repeating that request must not start another process. Do not retry cancellation, authentication, billing or safety failures, invalid output, or model-policy failures.

If the stage reports `failure_kind: anthropic_limit`, do not retry it. Use one fresh native Codex subagent with no inherited task conversation as the read-only fallback reviewer, then submit the same structured contract through `record_review_resolution`. Disclose origin `codex_fallback`. Other failures leave the review gate open.

Verify and deduplicate every finding before reporting it. Choose the verdict after this classification: `pass` requires no actionable finding, including P3; `blocked` retains confirmed findings and names a real blocking question. Record each disposition and evidence through `record_review_resolution`. Do not edit files, resolve review threads, post comments, commit, or push unless the user explicitly asks for changes.

If the user wants colleague-facing comments, load the `delivery-writing` review-comment reference. Keep the detailed evidence in chat, reduce each confirmed inline comment to one short actionable sentence, show the exact wording for approval, and prefer a reply in an existing thread over a duplicate comment.
