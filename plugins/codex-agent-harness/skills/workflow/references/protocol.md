# Durable workflow protocol

Use this protocol for one repository-changing task. The MCP server persists evidence but never executes project commands; Codex runs commands through its normal sandbox and records bounded outcomes.

## 1. Preflight and workspace

1. Read applicable `AGENTS.md`, `CLAUDE.md`, package scripts, and project documentation.
2. Inspect Git status and configuration that may cause data egress before running tests, workers, migrations, servers, or application commands.
3. If the checkout is dirty and the task does not target those changes, create an isolated detached worktree at the current `HEAD` and work there. Do not edit the dirty checkout.
4. If the task must overlap files already modified by the user, stop before writing and report `needs_human`; continue only after the user resolves that ownership boundary.
5. Call `check_runtime`. It must not invoke a model. A failed Claude check does not prevent Codex implementation, but the run cannot complete the independent-review gate.

## 2. Freeze the contract

Before editing, call `create_run` with the goal, non-goals, observable `done_when` criteria, constraints, forbidden actions, risk, and checks discovered from repository guidance. Default to `writer: codex`, `risk: medium`, and one correction pass.

Use `writer: claude` only after an explicit current-task request and set `writer_explicit: true`. Never ask one provider to be both writer and independent critic.

The contract is immutable. If the goal or acceptance criteria materially change, finish the current run as `needs_human` and create a new run after the user confirms the expanded scope.

## 3. Write and check

Implement the requested outcome as Codex unless the contract names Claude. Preserve unrelated changes and do not commit unless the current task authorizes it.

Call `plan_checks` after the diff exists. Run every returned command exactly as an argv array, subject to the normal sandbox and egress audit. Record each result with `record_check`; include only a short sanitized summary, never raw logs or secrets.

The server binds check results to the current diff fingerprint. Any later edit makes old evidence stale. Do not begin review until every required check passes for the current fingerprint.

## 4. Independent review

For a Codex-written change, delegate one `critic` stage to a fresh native tracking subagent. The proxy calls `start_stage`, polls with `poll_stage`, and cancels only when asked or when the task is abandoned. Claude reads the repository and Git directly; do not paste the diff into its prompt.

Codex verifies every returned finding against the code and records dispositions with `record_review_resolution`:

- `accepted`: the finding is valid; set `resolved: true` only after the correction exists.
- `rejected`: the finding is invalid or out of scope; include concrete contrary evidence and set `resolved: true`.
- `unverified`: the evidence is insufficient; keep `resolved: false`.

If Claude wrote the change, Codex performs the independent review and supplies that structured review to `record_review_resolution`.

## 5. One correction and completion

At most one correction pass is allowed. After an accepted finding is fixed, call `plan_checks` again and rerun every required check for the new fingerprint. Do not launch a second Claude critic in v1.

Call `finish_run` with `complete` only when the server confirms that current checks pass, an independent review exists, all findings are resolved, and no blocking question remains. Use `needs_human` for scope expansion, unresolved P0/P1 findings, dirty-file ownership, or a required user decision. Use `blocked`, `failed`, or `interrupted` only for their literal terminal conditions.
