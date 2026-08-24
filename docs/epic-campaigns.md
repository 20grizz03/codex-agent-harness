# Epic campaigns

Version 2.1 adds a local campaign above the existing v1 run. A campaign orders several implementation, analysis, or delivery tasks without adding a scheduler: Codex uses its native plan and calls the existing v1 workflow for every repository-changing task.

Codex coordinates the campaign and assigns each ready implementation task to a separate native subagent, with at most three task executors active at once. Sequential same-repository tasks accumulate without intermediate commits in one integration worktree. A terminal group of dependency-free tasks may also run from the same base SHA in separate worktrees when their expected paths do not overlap. Each task commits before its final v1 checks and review. A predeclared integration run freezes the source commit SHAs, verifies their Git ancestry, and runs the full v1 checks and review again. The candidate is sealed after integration and before any further edits.

## State

Campaign state is private Git metadata:

```text
<absolute-git-dir>/codex-agent-harness/campaigns/<campaign-id>/
├── contract.json
├── state.json
├── events.jsonl
└── comparison.json
```

The contract freezes the source reference, goal, constraints, ordered tasks, risk, and forbidden actions. Every implementation task also freezes its repository worktree and base SHA. Campaign creation requires a clean coordinating worktree. `state.json` tracks task status, completed v1 run references, sanitized human-intervention summaries, and the sealed candidate. `comparison.json` remains empty for normal delivery and until a replay candidate has been sealed.

The state path is `prepared → executing → candidate_ready → comparing → complete`. Normal delivery skips `comparing`. Terminal alternatives are `needs_human`, `blocked`, `failed`, and `interrupted`.

## MCP tools

- `create_campaign`, `get_campaign`, and `list_campaigns` manage the immutable contract and current state.
- `record_campaign_task` enforces dependency order. An `implementation` task can complete only with a unique terminal `complete` v1 run from its frozen worktree and base SHA. A `blocked`, `failed`, or `interrupted` task may be restarted through `in_progress` without discarding campaign history.
- `record_campaign_intervention` stores short summaries so blocking questions, approvals, corrections, and supplied context can be counted without retaining raw dialogue.
- `seal_campaign_candidate` freezes the agent's own result after every task and blocker is closed. It records a Git fingerprint per worktree and rejects changed paths that are not covered by completed v1 runs.
- `record_campaign_comparison` accepts only bounded scores, summaries, risks, and historical references after a replay candidate is sealed.
- `finish_campaign` enforces the candidate and replay-comparison gates. Its result never authorizes Jira, GitHub, deployment, or migration actions.

## Jira and GitHub Enterprise

The plugin process contains no Jira or GitHub client. The `epic-workflow` skill uses purpose-built connectors already available to Codex for issue and PR metadata, and local Git over SSH for repository history. External writes always require a separate explicit user instruction.

For a closed-epic replay, a curator reconstructs the input at a cutoff before implementation. The executor cannot use final statuses, late comments, testing recommendations, linked PRs or commits, or historical diffs until its candidate is sealed. A fresh evaluator then compares scope, behavior, architecture, tests, and operability on a 0–4 rubric.
