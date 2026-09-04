# Durable workflow protocol

Use this protocol for one repository-changing task. The MCP server persists evidence but never executes project commands; Codex runs commands through its normal sandbox and records bounded outcomes.

## 1. Preflight and workspace

1. Read applicable `AGENTS.md`, `CLAUDE.md`, package scripts, and project documentation.
2. Inspect Git status and configuration that may cause data egress before running tests, workers, migrations, servers, or application commands.
3. If the checkout is dirty and the task does not target those changes, create an isolated detached worktree at the current `HEAD` and work there. Do not edit the dirty checkout.
4. If the task must overlap files already modified by the user, stop before writing and report `needs_human`; continue only after the user resolves that ownership boundary.
5. Call `check_runtime`. It must not invoke a model. A failed Claude check does not prevent Codex implementation, but the run cannot complete the independent-review gate.

## 2. Classify and approve the implementation plan

Before `create_run` and before the first repository edit, classify the task by behavior, not by the number of Jira issues or the length of the proposed plan.

Use this direct workflow only for a narrow, unambiguous change whose observable result, affected area, important boundaries, and checks can be stated without choosing a new product or architectural contract. OpenSpec is mandatory when even one task changes any of these:

- concurrency, ordering, isolation, or coordination between operations;
- retry, timeout, recovery, or duplicate-processing behavior;
- partial success, partial failure, error aggregation, or completion semantics;
- idempotency, atomicity, consistency, security, sensitive-data handling, or compatibility;
- behavior across multiple external integrations or repositories;
- a material architectural choice or unresolved requirement.

One ready Jira issue is not automatically simple. For example, “run Google, Facebook, and Yandex independently, retry failed work, preserve partial success, and aggregate errors and metrics” requires OpenSpec even if Jira contains it as one issue. Switch to `epic-workflow`, materialize and validate the local OpenSpec, and create a one-task campaign before its implementation run.

For a direct task, show one concise plan that names the observable result, affected area, important non-goals or boundaries, and checks. Include failure and recovery behavior whenever they are relevant. A list of filenames or implementation steps alone is not an adequate plan. When the result will be delivered through Jira or a pull request, also state that local completion will create a fresh user-visible Codex publication task; approval of the plan is the explicit authorization to create that task later.

The direct task must contain one observable functionality and normally map to one independently publishable pull request. Aim for 300–700 changed production-code lines, with no lower bound for a naturally small change; report tests, documentation, configuration, generated files, and binaries separately. If the estimate is above 700, re-evaluate whether the work contains several useful behaviors and switch to `epic-workflow` with OpenSpec when it does. Keep an indivisible behavior together when splitting would break buildability, testability, safe rollout, or rollback; record that reason without escalating risk solely because of size.

If the user already supplied a concrete plan and explicitly asked to implement it, that request is the approval; do not ask again. It authorizes a later publication task only when the supplied plan explicitly includes one. When OpenSpec is required, still materialize and show the equivalent artifacts before code, but wait again only if this exposes a new product decision or materially changes the approved contract.

Approval authorizes reversible local edits, checks, corrections, and commits within that plan. Do not request separate permission to write code. Ask again only when the product contract or scope materially expands, the task overlaps user-owned dirty files, or an external or irreversible action is required.

## 3. Freeze the contract

Before editing, call `create_run` with the goal, non-goals, observable `done_when` criteria, constraints, forbidden actions, risk, checks discovered from repository guidance, and a frozen `review_budget`. Default that budget to expected production lines `0..700`, a soft maximum of 700, and no exception reason; default to `writer: codex`, `risk: medium`, and one correction pass. For an epic task also pass its `campaign` reference; the server resolves its base, inherits the task budget and campaign risk, and freezes the active plugin version. A campaign-linked run cannot expand its budget.

Use `writer: claude` only after an explicit current-task request and set `writer_explicit: true`. Never ask one provider to be both writer and independent critic.

The contract is immutable. If the goal or acceptance criteria materially change, finish the current run as `needs_human` and create a new run after the user confirms the expanded scope.

## 4. Write and check

Implement the requested outcome as Codex unless the contract names Claude. Preserve unrelated changes and do not commit unless the current task authorizes it.

Call the read-only `measure_diff` after each coherent slice and before adding another behavior when production changes approach 500 lines. A result above the soft limit does not block writing, checks, review, or completion. Re-evaluate the functional boundary: reduce a mixed task, or keep a cohesive change and carry the concrete reason into the publication package. Call `plan_checks` after the diff exists; it persists the categorized counts for the current fingerprint. For a campaign task that is a later `base_from_task` predecessor, create its already-approved atomic local commit before the final check plan so the next task receives a clean base. If review correction is needed, amend that commit once and rerun all checks. Run every returned command exactly as an argv array, subject to the normal sandbox and egress audit. Record each result with `record_check`; include only a short sanitized summary, never raw logs or secrets.

The egress audit restricts execution, not repository editing. Shared queues, databases, mail, APIs, or other endpoints never revoke an approved local implementation plan. Continue writing the code, then isolate the command with safe configuration. If that is impossible, leave it unrun and report the resulting completion blocker; an additional safe check does not erase a mandatory result.

The server binds check results to the current diff fingerprint. Any later edit makes old evidence stale. Do not begin review until every required check passes for the current fingerprint.

## 5. Independent review

For a Codex-written change, prefer delegating one `critic` stage to a fresh native tracking subagent. Its task must explicitly forbid starting any MCP server or process, including `scripts/mcp_server.py`, and require it to return exactly `lifecycle tools unavailable` without side effects when `start_stage`, `poll_stage`, or `cancel_stage` is absent. Otherwise it calls `start_stage`, polls with `poll_stage`, and cancels only when asked or when the task is abandoned. After the unavailable response, the Codex instance that owns the current run, including a campaign executor, performs those calls through its already configured `agent-harness` MCP server. Never start another MCP server or process for the same run. Claude remains fresh and reads the repository and Git directly; do not paste the diff into its prompt or expose its raw stream to the run owner.

Do not retry the Claude stage. If and only if its terminal state contains `failure_kind: anthropic_limit`, delegate the same read-only review contract to one fresh native Codex subagent with no inherited task conversation. A campaign cooldown may produce that terminal state without invoking Claude; it is still an explicit degraded review. The server permits one probe after cooldown expiry and restores Claude after a successful probe. Supply the fallback result to `record_review_resolution`; the server records origin `codex_fallback`. A timeout, authentication problem, process failure, invalid output, cancellation, or quality-floor violation never enables fallback.

Codex verifies every returned finding against the code and records dispositions with `record_review_resolution`:

- `accepted`: the finding is valid; set `resolved: true` only after the correction exists.
- `rejected`: the finding is invalid or out of scope; include concrete contrary evidence and set `resolved: true`.
- `unverified`: the evidence is insufficient; keep `resolved: false`.

If Claude wrote the change, Codex performs the independent review and supplies that structured review to `record_review_resolution`.

## 6. One correction and completion

At most one correction pass is allowed. First record an accepted finding with `resolved: false`, then fix it, call `plan_checks` again, and rerun every required check for the new fingerprint. Finally update that finding to `resolved: true` against the checked correction. Do not launch a second critic.

Call `finish_run` with `complete` only when the server confirms that current checks pass, an independent review exists, all findings are resolved, and no blocking question remains. Use `needs_human` for scope expansion, unresolved P0/P1 findings, dirty-file ownership, or a required user decision. Use `blocked`, `failed`, or `interrupted` only for their literal terminal conditions.

## 7. Hand off for publication

Include the persisted `diff_stats` and `budget_status` in the compact handoff, so publication can show the categorized size and any cohesive overage without reusing the implementation conversation.

The required boundary is a fresh user-visible task that does not inherit implementation history. In the current Codex app the user-facing lead creates it with `create_thread`; a campaign executor returns to the campaign lead. Do not use `fork_thread`, because a fork retains the history this boundary is intended to discard. If fresh-task creation is unavailable, show the compact handoff in the current task and ask the user to open a new one; do not fork the context or perform external writes.

For a direct workflow, create the publication task only after `finish_run(status: complete)` and only when its creation was explicitly included in the approved plan. In a campaign, create it as soon as the run for an independently publishable Jira task is complete and recorded; do not wait for sibling tasks, candidate sealing, or `finish_campaign`. If several implementation tasks intentionally form one pull request, wait only for their integration run to complete. Before handoff, a campaign candidate must be fully committed on its own branch or kept in an isolated worktree that no active run will mutate; never hand off an uncommitted shared worktree while the campaign continues. If creation was not approved in advance, ask once and remain in the current context without external writes until the user answers.

Start the new task prompt with the exact structured role described in [the publication-context reference](../../delivery-writing/references/publication-context.md): `publication_context.active: true`, a stable `scope_id`, `source_run_ids`, and the source issue or local reference. Under it pass one `candidates` block per run. Keep that run's workspace, repository, candidate worktree, base and final SHAs, committed state, source branch or explicit absent value, remote, pull-request target, proposed source branch for a detached worktree, current `diff_fingerprint`, implemented outcome, changed paths, completed checks, independent-review verdict and origin, and residual risks inside the same block. Keep only shared campaign metadata and proposed external actions at package level. For a campaign also include its workspace, `campaign_id`, and task IDs. Do not include the full conversation, raw diff, command output, model report, or tool transcript. Branch creation for a detached direct-workflow candidate remains a proposed publication action; never create or commit from a shared campaign worktree with active runs. If a run is `needs_human`, has unresolved findings, or lacks current green checks, keep it in the implementation context. Do not create a second publication task for the same `run_id` or `scope_id`; continue the existing one. If the user explicitly adds another verified candidate to that publication package, append its candidate block in the existing publication task instead of handing both candidates to another task.

In the fresh task, load `delivery-writing` and call `get_run` with the paired workspace and `run_id` from every candidate block; for a campaign also call `get_campaign`. Each run must be terminal `complete`, have green current checks, a resolved review with the stated origin, matching changed paths, and the handed-off current `diff_fingerprint`. Then verify each candidate's Git state and independently read its diff against its own frozen base. Use `<base>..<final>` when the final commit contains the complete change. An uncommitted candidate is allowed only for a direct workflow or an isolated worktree that no other run can mutate; inspect `git diff <base>`, `git status --porcelain=v1 --untracked-files=all`, and every new file. Any state, fingerprint, path, or Git mismatch excludes that candidate and stops preparation of the whole package; never show or authorize a partially verified package. Before composing the package, follow the delivery-writing [local manual check](../../delivery-writing/references/manual-smoke.md) against every verified candidate. A reproducible contract mismatch stops the package and starts a new `workflow`; non-applicability, no safe local method, or an unsafe environment is documented but does not block it. Recheck Git and the diffs afterwards, and repeat verification and the scenario if a candidate changed. Build the text from verified behavior rather than from the handoff summary. Refresh current Jira, branch, pull-request, and check state before proposing external writes. Show one publication package with:

- a concise account of what was implemented;
- a `Локальная ручная проверка` section with the scenario, local method, observed result, status, and limitation;
- the exact commit message or existing commit SHA;
- the pull-request title and one- or two-sentence description;
- the complete Jira testing-recommendations block;
- the exact actions that the confirmation will perform.

After that package is visible, any unambiguous explicit user instruction authorizes only the named subset of its listed local branch or commit, push, pull-request, and Jira testing-recommendation actions. The instruction need not literally be `публикуем`: for example, `пушим поверх` authorizes the shown commit and ordinary push without `--force`, while `создавай PR` authorizes only the shown pull-request action. If the instruction names a subset, perform only that subset. It does not authorize merge, task-status changes, deployment, migrations, unlisted comments, unseen modifications, or manual checks in an external or shared environment.

Refresh branch, pull-request, check, and Jira state before the first external write. If the package has materially changed, show the updated package and wait for a new confirmation. After a transient failure, retry the same authorized action without another confirmation only while the candidate, target branch, and action set remain unchanged. A changed diff or commit, another branch, force-push, or an additional external action requires an updated package and confirmation. Keep the same publication task for pull-request checks, CI failures, review feedback, deployment status and deployment-failure diagnosis for this Jira task. A code correction starts another durable `workflow` from this focused context; an actual deployment or migration still requires its own explicit authorization. Do not create another user-visible task for these follow-ups.
