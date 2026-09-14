# Correction chains

The follow-up journal coordinates a corrected source candidate and its dependent branches without rewriting completed runs. One owner controls each chain. The MCP tools store bounded state in `<git-common-dir>/codex-agent-harness/followups/<followup-id>/journal.json` and perform read-only Git checks; they do not create worktrees, merge, replay commits, run commands or models, send messages, or authorize publication.

## Freeze a chain

`create_followup` takes one clean, pinned source and 1–64 descendant tasks in topological order. All worktrees must belong to the same Git repository and be distinct. Each task freezes its complete `base_sha..head_sha` range, publication and active-writer flags, dependencies, and nonempty build/test check-name lists. SHAs must be full 40- or 64-character lowercase commit IDs; the task worktree must be at its declared `head_sha`. `parent_task_id` selects the Git parent when a task has multiple dependencies. Replace every placeholder below with a real absolute path or full SHA; this is a shape example, not an executable request.

```yaml
workspace: /repo/worktrees/source
followup_id: parent-fix-1
owner_id: epic-lead
source:
  workspace: /repo/worktrees/source
  base_sha: <full source base SHA>
  head_sha: <full corrected source SHA>
tasks:
  - id: service
    workspace: /repo/worktrees/service
    base_sha: <full service base SHA>
    head_sha: <full service head SHA>
    published: true
    active: false
    run_id: run-service
    build_checks: [service-build]
    test_checks: [service-contract]
    check_bindings:
      service-build: <SHA-256 of command and conditions descriptor>
      service-contract: <SHA-256 of command and conditions descriptor>
  - id: client
    workspace: /repo/worktrees/client
    base_sha: <full client base SHA>
    head_sha: <full client head SHA>
    dependencies: [service]
    parent_task_id: service
    published: false
    active: true
    build_checks: [client-build]
    test_checks: [client-contract]
    check_bindings:
      client-build: <SHA-256 of command and conditions descriptor>
      client-contract: <SHA-256 of command and conditions descriptor>
```

`run_id` is optional. When supplied, it must identify a complete run for the task's exact baseline. The baseline must be clean and committed. A commit made after review may change Git metadata: in that case all files and Git modes must exactly match the completed run's captured snapshot. The original run fingerprint stays historical and is never rewritten. Only such a verified baseline permits `mechanical`; a task without it uses `semantic` and a new scoped run. An active writer first releases a safe `checkpoint`; its original worktree, index, commits, and old run remain intact. If the checkpoint is dirty, before `begin` make an exact clean snapshot commit in a separate adaptation worktree, but only when local commits were already authorized. The server compares the complete file manifest and Git modes. That snapshot commit joins the full own-commit range and must survive replay. Without commit authority, preserve the snapshot and report that precise boundary.

## Record transitions

Each `record_followup` call supplies `workspace`, `followup_id`, the frozen `owner_id`, `expected_revision`, a stable `request_id`, an `action`, and action-specific `data`. A new request must match the current revision; repeating the same `request_id` with the same payload is idempotent. Reusing it with different content fails. Initial state has `revision: 0` and `epoch: 1`; each accepted action increments the revision and updates UTC `updated_at`. `begin` increments that task's `attempt` and pins the parent SHA and a distinct adaptation worktree. `queue_source` records a newer clean descendant source; `advance` starts the next epoch only at safe task states. A queued source never silently replaces the parent of an in-flight attempt.

Freeze a `check_bindings` digest for every build/test name. Compute SHA-256 over a locally retained canonical UTF-8 JSON descriptor (`sort_keys=True`, separators `,` and `:`) with `argv`, candidate-relative `cwd`, toolchain version, fixture identifiers and relevant environment/configuration conditions. Do not include raw credentials in the descriptor or journal. A `check` result supplies this digest as `check_fingerprint` plus the current `diff_fingerprint`; both must match. Recompute the descriptor before running/reusing a check. Changed conditions require a new journal contract, not a green result under the old name. The journal records attestations; it never executes or observes commands itself.

| Action | State effect |
| --- | --- |
| `checkpoint` | The active writer explicitly releases a safe snapshot; `awaiting_checkpoint → pending`. |
| `begin` | After any dirty snapshot commit, pin the parent and start `mechanical` or `semantic`; `pending`/`blocked`/`interrupted → adapting`. |
| `candidate` | Check the clean committed result, parent ancestry, and full own-commit provenance, including correction and snapshot commits; `adapting → checking`. `replayed_commits` maps old to new SHAs when needed. |
| `check` | Record a current candidate fingerprint, command/conditions fingerprint, check name, exit code, and duration. The journal does not accept command arguments or logs. |
| `finish` | Require each affected PR's own build. Terminal candidates also require affected tests for their accumulated ancestor scope. `mechanical → mechanical_checked`; `semantic → complete` only with its own complete, current run. |
| `pause` | Preserve an adapting/checking snapshot as `blocked` or `interrupted`. |
| `reopen` | With a reason in `summary`, return a finished task and its affected finished descendants to pending, preserving candidates as baselines and clearing current checks/run authority. Active descendants must pause first; their checkpoints remain intact. Attempts keep increasing within the same epoch, without a new source SHA. |
| `abandon` | At safe checkpoints, cancel a mistaken or unwanted chain with a reason in `summary`. Retain all history and results, release reservations, and reject later mutations or current run/publication authority. Repeating the original request is idempotent. It does not stop agents/models or delete files. |
| `queue_source`, `advance` | Queue a newer source, then advance when tasks are ready, pending, or blocked/interrupted at an unchanged checkpoint. A paused unfinished copy becomes the new baseline and continues only through `semantic` and a fresh run. |
| `progress` | Accumulate bounded milliseconds in `writing`, `propagation`, `checks`, `review`, or `external_wait`; external waiting does not count toward the 15-minute active-work report. |

Codex executes Git operations and checks outside these tools. Test names must be unique across tasks and distinct from build names, so a leaf build cannot silently satisfy an ancestor's test. Published branches receive an ordinary merge that preserves their existing history; unpublished branches replay their complete own range, including prior corrections and any snapshot commit. A truly identical integrated commit may be mapped without duplicating its tree. Build each affected PR; run affected tests on terminal candidates with their ancestors' accumulated check names, not redundantly on intermediate branches or as an extra chain-wide build. Commit mappings account for the complete range; they do not prove semantic equivalence, which Codex must check against the accepted contract.

Current and queued source worktrees are read-only inputs, including when shared across chains; never allocate them for adaptation. Within a chain, queued sources and adaptation worktrees exclude one another in both allocation orders. `advance` revalidates the promoted source against its pinned snapshot. Before `abandon`, stop active agents/models and save their safe checkpoints; cancellation releases only the journal's logical reservations, not ownership or authority over user files.

A semantic run's immutable `create_run.followup_ref` contains `workspace`, `followup_id`, `task_id`, `epoch`, `attempt`, and `parent_sha`. `finish` checks that run's completed state, current code (including the exact post-review commit case), base SHA, and exact reference. A paused semantic attempt cannot downgrade to mechanical. Resuming preserves all checkpoint commits and dirty files before adaptation. An old run or `mechanical_checked` cannot stand in for independent review of changed behavior.

`get_followup` returns stored `status`, `transfer_ready_tasks` with pinned parents, and freshly computed publication `readiness`. They are different: even `mechanical_checked` or `complete` can be unready when Git changed, a dependent candidate or leaf test is missing, a source is queued, or the source moved. Its `progress_checkpoint_due` reflects 15 minutes of recorded active work; `external_actions_authorized` is always false. Publish only the current candidate after checking its exact follow-up reference and evidence. A changed candidate or target needs a newly shown publication package and explicit authority for the external action; no follow-up tool grants PR, push, merge, deployment, or force-push permission.
