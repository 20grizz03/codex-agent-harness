# Epic campaigns

An Agent Harness campaign orders implementation, analysis, integration, and delivery tasks without adding a scheduler. Codex uses its native plan and delegates each repository-changing task to a separate durable run.

## Prepared task contract

The campaign freezes its source, goal, restrictions, risk, OpenSpec fingerprint, runtime version, and ordered tasks. A prepared task may contain:

- immutable `goal` and `done_when`, plus `constraints`, `non_goals`, and `required_checks`;
- pinned `contract_refs` objects with required `ref` and `revision` values;
- `execution.native_model`, `execution.reasoning_effort`, and optional `execution.escalation_model`;
- `review_budget`, `max_correction_passes`, and `max_critic_retries`;
- `wave`, local `dependencies`, `base_sha` or `base_from_task`, and publication `dependency_strategy`;
- repository workspace, role, kind, and expected publication boundary.

New tasks default to `gpt-5.6-sol` with `high` reasoning effort, two correction passes, and one transient critic retry. A legacy missing correction value remains one pass; a legacy missing critic-retry value remains zero. The runtime default for `execution.escalation_model` is null; an epic plan may explicitly set `gpt-6-astra`. The lead passes the stored native model and effort explicitly when spawning the executor. These values are planning metadata: MCP stores them but does not launch or attest the native model.

A campaign-linked run must use the task goal. It inherits completion criteria, constraints, non-goals, forbidden actions, required checks, pinned references, execution settings, review budget, and recovery budgets. The caller may add criteria, constraints, non-goals, forbidden actions, checks, and references, but cannot override a same-name check or any frozen setting. The approved OpenSpec becomes a pinned `openspec:<change-id>` reference using its semantic SHA-256.

## Readiness and OpenSpec

OpenSpec is required for a large, ambiguous, high-risk, multi-task, or behaviorally complex change. Readiness requires affected components, contract revisions and availability, states and errors, recovery, security, compatibility, rollout, publication, and rollback boundaries. Every requirement maps to at least one task; every task maps to a verification scenario. Ordinary technical choices can remain for the lead. Product, scope, or task-boundary uncertainty becomes an `analysis` task before implementation.

The default local OpenSpec mode keeps `/openspec` ignored and snapshots the approved change in private Git metadata. Explicit `storage: repository` preserves versioned OpenSpec and its finalization run. Checkbox state is normalized; another content change blocks task progress and candidate sealing. The 300–700 production-line budget remains advisory and is reported separately from tests, documentation, configuration, generated files, and binaries.

## Waves and integration

`wave` and `dependencies` control local implementation. Independent tasks in one wave may run from the same base in separate worktrees when expected paths do not overlap. New campaigns use `combined-review-when-needed`: combining several slices in a wave requires a predeclared `kind: implementation`, `role: integration` task with full checks and independent review. A single-slice wave instead passes its completed, clean, verified source SHA directly as `base_from_task`; there is no synthetic merge or duplicate review of an identical tree. Legacy `combined-review-required` contracts retain their existing integration gates.

`dependency_strategy: parallel | stacked | after_merge` describes PR publication order. It does not replace the local dependency graph. In particular, `after_merge` can defer external publication while local implementation proceeds from a ready verified base. Integration uses `report_only` and is not a product PR.

At most three implementation executors run at once. Every executor gets its stored model and effort, its own worktree, run, checks, review history, and atomic commit. Campaign state records task transitions and sanitized interventions; it never stores raw connector responses, model streams, credentials, or environment values.

## Checks, review, and recovery

The run fingerprint covers the full result from the frozen base: committed changes, index, working tree, and untracked files without mutating the index. Any change invalidates earlier checks and current review.

Code corrections are bounded by `max_correction_passes`. Begin a correction with `plan_checks(begin_correction: true)`. Each corrected fingerprint reruns all checks and receives a new independent review scoped to previous findings, the delta and affected relationships when the reviewed clean SHA is available; otherwise use full review. Prior cycles remain in `history`. Several edits before the next check/review boundary count as one correction pass.

An explicit `plan_checks(nonsemantic_closeout: true)` permits only accepted editorial P3 corrections in supported existing documentation files or changes to `// indirect` markers in root `go.mod`. File hashes and modes must prove there are no other changes. Required checks rerun, all findings must close, and original review provenance remains visible as `terminal.verification_reuse`; no model correction pass is consumed. This does not permit new code, changed module versions, instructions, or contracts, and is unavailable without a captured review snapshot. See [review follow-ups](../plugins/codex-agent-harness/skills/workflow/references/review-followups.md).

A critic stage may be retried once only after `transient_timeout` or `transient_process_failure`, by passing the failed stage as `start_stage.retry_stage_id`. The new stage records `retry_of`; repeating the request is idempotent. Authentication, billing, safety, cancellation, invalid output, and model-policy failures are not retryable. A confirmed `anthropic_limit` remains separate: it opens the shared campaign cooldown and permits an explicitly marked fresh `codex_fallback`; one later model stage probes after the cooldown. `AGENT_HARNESS_ANTHROPIC_COOLDOWN_SECONDS` defaults to 3600 and accepts 1 through 14400 seconds. During cooldown a critic receives the marked fallback path, while an explicitly selected Claude writer ends with an operational failure and no fallback. There is no generic provider fallback.

## State and tools

Campaign files live under `<git-common-dir>/codex-agent-harness/campaigns/<campaign-id>/`; run files live under the target worktree's absolute Git directory. `contract.json` is immutable, `state.json` is atomically replaced, and `events.jsonl` is append-only. Run state tracks `review_cycle` and `critic_retries`; each current review records its `cycle`, `diff_fingerprint`, and Claude `stage_id`, while earlier reviews remain in `history`. The campaign state path is `prepared → executing → candidate_ready → comparing → complete`, with terminal operational alternatives.

`create_campaign`, `get_campaign`, and `list_campaigns` manage the campaign. `record_campaign_task` enforces the dependency graph, verified bases, run completion, and integration. `record_campaign_intervention` stores only actual context, corrections, approvals, external unblocks, and blocking questions. `needs_human` requires a previously recorded unresolved blocking intervention; use `blocked` or `interrupted` for operational failures. `seal_campaign_candidate` freezes covered Git results. Replay then uses `record_campaign_comparison`; `finish_campaign` reports `evaluated` for replay and `locally_ready` for delivery. None authorizes an external write.

Long campaigns may update the plugin only between task waves, with no task in progress and no active Anthropic probe. Each run records its runtime version, and the server rejects a detectable downgrade.

## Publication

The current user-visible task is the default publication context. An independently publishable candidate may be prepared after its run completes; a shared PR waits for the complete integration run. A separate fresh task is created only when the user explicitly chooses that boundary, never merely because the work is ready to publish.

`publication_context.active: true`, its stable `scope_id`, and all `source_run_ids` remain mandatory. Each candidate block keeps its run workspace, repository, immutable worktree, base and final SHAs, branch and remote data, fingerprint, changed paths, check evidence, review origin, diff statistics, and budget status. The publication context verifies every run and reads every complete Git result from its own base. One mismatch stops the whole package.

After local manual verification, the task shows the implementation summary, commit, PR text, Jira testing recommendations, and exact external actions. An unambiguous user instruction authorizes only the named subset. The authorization persists for an unchanged reversible action and its retry after a transient failure. Merge, Jira status, deployment, migration, force-push, and unlisted comments require explicit authority.

For a narrow low-risk follow-up in an active publication task, the [small publication fix route](../plugins/codex-agent-harness/skills/delivery-writing/references/publication-context.md#мелкие-доработки) replaces automatic tests, manual smoke, and independent review with explicit unverified-delta reporting. Baseline evidence remains immutable and is not attributed to the new code; only the recorded follow-up explains the fingerprint difference. The updated package needs publication approval. This route cannot satisfy campaign completion, integration, next-wave prerequisites, or replay sealing.

## Closed-epic replay

A replay curator reconstructs only the cutoff input. Executors cannot read final statuses, late comments, testing recommendations, linked PRs or commits, or historical diffs before `seal_campaign_candidate`. A fresh evaluator then scores contract fidelity and historical similarity, attributes gaps, and reports candidate readiness. The same prepared-task contracts, waves, checks, review history, cooldown, and recovery rules apply without weakening the historical repository's required checks.
