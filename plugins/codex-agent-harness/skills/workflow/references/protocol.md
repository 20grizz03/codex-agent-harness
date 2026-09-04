# Durable workflow protocol

Use this protocol for one repository-changing task. The MCP server persists evidence but never executes project commands; Codex runs commands through its normal sandbox and records bounded outcomes.

## 1. Preflight and workspace

Read applicable `AGENTS.md`, `CLAUDE.md`, project scripts, and repository guidance. Inspect Git status and configuration that may cause data egress before running project commands. Isolate unrelated dirty work in another worktree. For an authorized reversible non-semantic overlap with clear intent, preserve a recoverable copy and merge safely. Stop with `needs_human` only when divergent intent or ownership cannot be determined.

Call `check_runtime`; it must not invoke a model. Failed Claude readiness does not block writing, but a standard run cannot complete without its independent-review gate.

## 2. Choose the proportionate path

Before `create_run`, classify the change by behavior and risk. OpenSpec is mandatory for the complex boundaries below. For example, one Jira issue that runs Google, Facebook, and Yandex independently with retries and partial success still needs an approved one-task campaign.

The concise direct path is available only when inspection makes all of these clear: the edit is low risk, small, non-executable, does not change product behavior or a maintained contract, and needs no architectural choice. Typographical fixes and stale prose can qualify. Code, tests, configuration, security policy, `AGENTS.md`, skill instructions, generated artifacts, and behavioral documentation do not qualify merely because their diff is small. State the edit, preserve unrelated changes, run every repository-mandated check that applies, inspect the final diff, and report that no durable run evidence was created.

Use a standard direct run for one narrow, unambiguous functionality. Show a concise plan with the observable result, boundaries, and checks. A typical slice contains 300–700 changed production-code lines, while tests, documentation, configuration, generated files, and binaries are counted separately. This is a soft planning budget; keep an indivisible behavior together and record why it cannot be safely split.

Use `epic-workflow` and approved OpenSpec when a task changes concurrency, ordering, retries, recovery, partial failure, idempotency, consistency, security-sensitive data, compatibility, several integrations, or a material architectural contract. The technical readiness check must map every requirement to at least one task and every task to a verification scenario. Every attached contract reference must have a pinned revision and its availability must be checked before implementation.

If the user supplied a concrete plan and asked to implement it, that is approval for reversible local work within its boundaries. The authorization persists across turns and covers edits, checks, bounded correction, approved commits, and unchanged retries. Resolve a reversible non-semantic file overlap when the user's intent is clear. The lead may resolve ordinary technical choices inside the contract. Ask only for a product or scope change, a user-owned-file conflict whose intent cannot be determined safely, or an external or irreversible action not already authorized.

## 3. Freeze the contract

Before editing on the standard path, call `create_run` with the goal, observable `done_when`, non-goals, constraints, forbidden actions, risk, required checks, pinned `contract_refs`, `review_budget`, `max_correction_passes`, and `max_critic_retries`. Each contract reference has the form `{ref, revision}`. New contracts default to two correction passes and at most one transient critic retry. A missing legacy correction value remains one; a missing legacy retry value remains zero.

For a campaign task, also pass its campaign reference. The server requires the linked goal to match the task and inherits `done_when`, constraints, non-goals, forbidden actions, checks, contract references, execution settings, review budget, correction budget, and critic-retry budget. The run may add criteria, constraints, non-goals, forbidden actions, checks, and references; it cannot override a same-name check or any frozen setting. The approved OpenSpec is inherited as a pinned `openspec:<change-id>` reference.

Prepared tasks store `execution.native_model`, `execution.reasoning_effort`, and optional `execution.escalation_model`. Default native execution is `gpt-5.6-sol` with `high` effort and a null escalation model; an epic plan may explicitly set `gpt-6-astra`. The lead passes the native model and effort explicitly to the executor. Execution fields are planning metadata: MCP does not launch or attest the native model.

Use `writer: claude` only after an explicit current-task request and set `writer_explicit: true`. Never use one provider as both writer and independent critic.

The contract is immutable. A material change to goal, product behavior, scope, or acceptance criteria requires a new approved contract; an ordinary technical decision inside those boundaries does not.

## 4. Write and check the full result

Implement as Codex unless the contract names Claude. Preserve unrelated changes and commit only when authorized.

Use `measure_diff` at useful slice boundaries. An `over_soft_limit` result prompts another boundary check but does not block completion. Call `plan_checks` after the diff exists. Its fingerprint covers the full result from the frozen base, including commits, index, working tree, and untracked files without changing the user's index. Run every returned argv through the normal sandbox and record a short sanitized result with `record_check`.

The egress audit restricts command execution, not approved local editing. Isolate unsafe project commands where possible; if a mandatory check cannot run safely, report the blocker. Any edit invalidates old checks for the previous fingerprint.

For a campaign dependency, create or update the approved atomic implementation commit before final checks so the integration task can verify it. A later wave begins only from the completed integration commit of its prerequisite wave.

## 5. Independent review and recovery

For a Codex-written change, use a fresh Claude `critic` after all checks pass. Prefer a native tracking subagent whose task forbids starting any MCP server or `scripts/mcp_server.py` and returns exactly `lifecycle tools unavailable` if lifecycle tools are absent. The run owner then uses its already configured server. Claude reads Git directly; do not paste the diff or expose raw model output.

If Claude wrote the change, Codex performs the independent review and supplies the structured result through `record_review_resolution`. Never ask Claude to review its own implementation.

If the critic ends with `failure_kind: transient_timeout` or `transient_process_failure` and the frozen retry is unused, the lead may explicitly call `start_stage` with `retry_stage_id` naming the failed stage. This creates at most one new critic stage. Repeating the same request is idempotent. Do not retry after cancellation, authentication, billing or safety failure, invalid output, or model-policy failure.

If and only if the terminal state contains `failure_kind: anthropic_limit`, use one fresh read-only native Codex reviewer with no inherited task conversation. A campaign cooldown may produce that state without invoking Claude; one later stage may probe after cooldown. Record the review origin as `codex_fallback`. This fallback remains distinct from transient retry and is never a generic provider switch.

Verify every finding and record its disposition:

- `accepted`: valid; resolve only after the correction exists;
- `rejected`: invalid or out of scope, with concrete contrary evidence;
- `unverified`: insufficient evidence, left unresolved.

Choose the review verdict after classifying findings. `pass` requires no actionable finding, including P3. `changes_requested` retains every confirmed finding. `blocked` requires a real blocking question and keeps any confirmed findings; never move a defect to `residual_risks` to obtain `pass`.

## 6. Bounded correction and completion

For each correction cycle, record accepted findings unresolved, fix them as one bounded pass, call `plan_checks(begin_correction: true)`, and rerun every required check. The changed fingerprint then requires a new independent review. Keep prior reviews and resolutions in history. Several edits before the next check/review boundary count as one correction cycle. Stop when immutable `max_correction_passes` is exhausted.

Call `finish_run(status: complete)` only for the current full-result fingerprint with passing checks, a current independent review, all findings resolved, and no blocker. Use `needs_human` for a product or scope decision or dirty-file ownership; use other terminal states for their literal operational conditions.

## 7. Prepare publication

The current user-visible task is the default publication context. A campaign executor returns to its campaign lead, which verifies the candidate and prepares publication there. Create a separate fresh task only when the user explicitly selected it for focus or isolation. Never require a fresh task merely because publication follows implementation, and never use `fork_thread` as a substitute.

Do not create a second publication task for the same `run_id` or `scope_id`; keep its verified package in the current selected publication context.

After a complete run, establish the structured `publication_context` described in [the publication reference](../../delivery-writing/references/publication-context.md). Include a stable `scope_id`, all `source_run_ids`, and one internally consistent entry in `candidates` per run. Keep `diff_stats`, `budget_status`, campaign metadata, residual risks, and proposed external actions. A campaign candidate must be committed or isolated from active writers.

Keep the package and any transfer limited to verified structured data. Do not include the full conversation, raw diff, command output, logs, model report, or tool transcript.

Load `delivery-writing`, call `get_run` for every candidate and `get_campaign` when present, then verify each full Git result against its own frozen base. Every candidate must have complete state, matching paths and fingerprint, green current checks, and a resolved current review. Any mismatch stops the whole package. Perform the local manual scenario, refresh external state, and show one package with the implementation summary, categorized size, manual check, commit, PR text, Jira testing recommendations, and exact proposed actions.

An unambiguous user instruction authorizes only the named subset of that shown package. It need not use a magic phrase. Refresh state before writing; if the candidate or action set changed, show the new package. Retry an already authorized action after a transient failure without asking again while its candidate, target, and scope remain unchanged. Merge, status changes, deployment, migration, force-push, and unlisted comments require their own explicit authorization.
