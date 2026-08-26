# Epic campaigns

Version 2.1 adds a local campaign above the existing v1 run. A campaign orders several implementation, analysis, or delivery tasks without adding a scheduler: Codex uses its native plan and calls the existing v1 workflow for every repository-changing task.

Codex coordinates the campaign and assigns each ready implementation task to a separate native subagent, with at most three task executors active at once. Sequential same-repository tasks end in clean atomic commits and the next task resolves its base through `base_from_task`, so each review stays focused. Dependency-free tasks may also run from the same base SHA in separate worktrees when their expected paths do not overlap. Two or more tasks in one repository require a predeclared `role: integration` run that reviews the combined diff from the original base. The candidate is sealed after integration and before any further edits.

## State

Campaign state and the approved local OpenSpec snapshot are private Git metadata shared by worktrees:

```text
<git-common-dir>/codex-agent-harness/campaigns/<campaign-id>/
├── contract.json
├── state.json
├── events.jsonl
├── comparison.json
└── spec/                 # local OpenSpec mode only
```

The contract freezes the source reference, goal, constraints, ordered tasks, risk, runtime version, and forbidden actions. A campaign-linked v1 run inherits at least that risk and records its own runtime version, base SHA, final HEAD, and diff fingerprint. An OpenSpec reference contains only its storage mode, change ID, repository-relative path, and a server-computed semantic SHA-256. The default `local` mode requires `/openspec` to be ignored by Git and privately snapshots the approved change; task transitions verify both copies. The explicit `repository` mode preserves versioned OpenSpec projects and accepts old references without a storage field. The custom schema requires behavior plus security/data, failure/recovery, operability, compatibility, and UI/source-material decisions. Checkbox state is normalized, while any other content change blocks task progress and candidate sealing. `state.json` tracks task status, provider cooldown, runtime-version checkpoints, sanitized human interventions, operational transition counts, and the sealed candidate.

The state path is `prepared → executing → candidate_ready → comparing → complete`. Normal delivery skips `comparing`. Terminal alternatives are `needs_human`, `blocked`, `failed`, and `interrupted`.

## MCP tools

- `create_campaign`, `get_campaign`, and `list_campaigns` manage the immutable contract and current state.
- `record_campaign_task` enforces dependency order, chained bases, minimum campaign risk, clean predecessor commits, and the combined integration run. `needs_human` additionally requires a real unresolved human action or decision; operational failures use `blocked` or `interrupted`.
- `record_campaign_intervention` stores only actual blocking questions, approvals, corrections, supplied context, and external unblocks. Status requests and task transitions are counted separately.
- `seal_campaign_candidate` freezes the agent's own result after every task and blocker is closed. It records a Git fingerprint per worktree and rejects changed paths that are not covered by completed v1 runs.
- `record_campaign_comparison` records cutoff-contract fidelity, historical similarity, gap attribution, residual risks, and `unsafe | partial | ready` candidate readiness after sealing.
- `finish_campaign` reports `evaluated` for replay and `locally_ready` for delivery. Its result never authorizes Jira, GitHub, deployment, or migration actions.

Campaign-linked model stages share an Anthropic cooldown. During the interval the server does not invoke Claude: a critic gets an explicit limit terminal that enables the bounded Codex fallback, while an explicitly selected Claude implementation ends as an operational failure without fallback. After expiry exactly one next model stage probes Claude; a successful probe closes the circuit and another confirmed limit extends it. The default interval is one hour and can be changed with `AGENT_HARNESS_ANTHROPIC_COOLDOWN_SECONDS` from 1 to 14,400 seconds. Plugin upgrades are allowed between task waves with no active probe, and every run retains the version with which it started; a detectable downgrade is rejected.

## Jira and GitHub Enterprise

The plugin process contains no Jira or GitHub client. The `epic-workflow` skill uses purpose-built connectors already available to Codex for issue and PR metadata, and local Git over SSH for repository history. External writes always require a separate explicit user instruction.

For a closed-epic replay, a curator reconstructs the input at a cutoff before implementation. The executor cannot use final statuses, late comments, testing recommendations, linked PRs or commits, or historical diffs until its candidate is sealed. A fresh evaluator separately scores fidelity to the cutoff contract and similarity to the historical result, attributes each gap, and states whether the candidate is safe to deliver.

## OpenSpec

For a live large idea, high-risk or ambiguous change, multi-task epic, or behaviorally complex single task, `epic-workflow` keeps the approved proposal, behavioral deltas, design decisions, and task list under the project's `/openspec`. Complexity includes concurrency, retries or recovery, partial-failure semantics, idempotency or consistency, security-sensitive data, compatibility, and coordination across external integrations. The default mode adds `/openspec/` to the repository's local Git exclude file, so specs stay beside the code without entering commits. A tracked `.gitignore` rule is also accepted. Only a narrow, unambiguous single task skips the separate change. OpenSpec does not replace campaign state: its checkboxes are the plan, while terminal task state and evidence remain in Agent Harness.

The first OpenSpec task checks actual dependency capabilities, including registry reachability, without requiring VPN as the mechanism. Local mode validates the change before campaign creation and again before sealing, but does not add an archive/finalizer task. Teams that explicitly choose `storage: repository` retain the separate `openspec-finalize` run and versioned archive. OpenSpec is not used for blind replay.
