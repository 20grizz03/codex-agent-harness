# Codex Agent Harness

`codex-agent-harness` is a local Codex plugin for durable repository-changing tasks and multi-task epic campaigns. Codex records immutable contracts, runs deterministic checks through its normal sandbox, obtains one fresh independent review per implementation run, and completes only against evidence tied to the current diff.

The plugin includes delivery-writing conventions: compact Jira tasks, post-implementation testing recommendations, one- or two-sentence PR descriptions, natural Russian technical prose, and concise colleague-facing review comments. Drafting text does not authorize changing Jira or publishing to GitHub.

Version 2.1 can read Jira epics and GitHub Enterprise delivery state through connectors already available to Codex. It can assign up to three dependency-free tasks to separate Codex subagents, including a terminal same-repository task group isolated in worktrees; a dedicated integration task rechecks and reviews their combined result. Every task keeps its own review. Claude remains the default critic; only a confirmed Anthropic usage limit permits one fresh read-only Codex fallback review, recorded explicitly as `codex_fallback`. Other Claude failures do not trigger retries or fallback.

## Skills

- `workflow` runs repository mutations through the durable Codex-led cycle.
- `review` obtains and verifies an independent read-only review.
- `setup` checks the Claude subscription runtime without model inference.
- `delivery-writing` prepares Jira tasks, post-implementation testing recommendations, concise PR descriptions, and review comments without publishing them.
- `epic-workflow` decomposes an epic into durable v1 runs and can perform a blind replay of a closed epic before comparing with historical Jira, PR, and Git evidence.

## Install on another computer

Install from the public repository using the native Codex marketplace flow:

```bash
codex plugin marketplace add 20grizz03/codex-agent-harness --ref main
codex plugin add codex-agent-harness@agent-harness-local
```

Ponytail is the default companion skill for keeping implementations small:

```bash
codex plugin marketplace add https://github.com/DietrichGebert/ponytail.git
codex plugin add ponytail@ponytail
```

Claude inference is subscription-only. Authenticate interactively when needed:

```bash
claude auth login
```

Start a new Codex task after installation so the skills and MCP tools are discovered from the installed snapshot. Ask Codex to check Agent Harness setup; the check does not invoke a model or install anything.

See [`plugins/codex-agent-harness/docs/dependencies.md`](plugins/codex-agent-harness/docs/dependencies.md) for the complete runtime, companion-skill, connector, and development-tool inventory bundled with the plugin.

## Install from a local checkout

Run from the repository root:

```bash
codex plugin marketplace add .
codex plugin add codex-agent-harness@agent-harness-local
```

Start a new Codex task after installation so the skills and MCP tools are discovered from the installed snapshot.

`check_runtime` reports readiness without invoking a model and refuses inference when API/provider-billing environment variables are active.

## Development

The plugin is under `plugins/codex-agent-harness`. It uses only the Python standard library and communicates with Codex over stdio MCP. Run the commands in `AGENTS.md` before a local commit.

Runtime state is stored per target worktree under:

```text
<absolute-git-dir>/codex-agent-harness/runs/<run-id>/
<absolute-git-dir>/codex-agent-harness/campaigns/<campaign-id>/
```

The worktree remains clean. Run and campaign contracts, atomic state, append-only events, reviews, and replay comparisons live in Git metadata with mode `0600`.

Repository owners can add path-based risk escalation and deterministic checks through [`docs/configuration.md`](docs/configuration.md). The configuration can only add gates or raise risk; it cannot weaken the contract frozen at run creation.

Epic campaign state and the replay boundary are documented in [`docs/epic-campaigns.md`](docs/epic-campaigns.md).
