# Codex Agent Harness

`codex-agent-harness` is a local Codex plugin for durable repository-changing tasks and multi-task epic campaigns. Codex records immutable contracts, runs deterministic checks through its normal sandbox, obtains one fresh independent review per implementation run, and completes only against evidence tied to the current diff.

The plugin includes delivery-writing conventions: compact Jira tasks, post-implementation testing recommendations, one- or two-sentence PR descriptions, natural Russian technical prose, and concise colleague-facing review comments. Drafting text does not authorize changing Jira or publishing to GitHub. Large ideas, high-risk or ambiguous changes, live multi-task epics, and behaviorally complex single tasks keep their approved decomposition in OpenSpec; Agent Harness remains the execution and evidence layer.

Version 2.1 can read Jira epics and GitHub Enterprise delivery state through connectors already available to Codex. It can assign up to three dependency-free tasks to separate Codex subagents. Sequential tasks use clean chained commits, while a dedicated integration task rechecks and reviews the combined repository result. Every task keeps its own review. As soon as an independent PR or Jira result is locally complete, it moves to a fresh user-visible publication task while the epic continues; that task stays with the delivery through CI, review feedback, and deployment diagnosis. Publication waits for the shown package and `публикуем`. Claude remains the default critic; a confirmed Anthropic usage limit opens a campaign cooldown with one later recovery probe and explicit `codex_fallback` reviews meanwhile. Other Claude failures do not trigger retries or fallback.

Implementation tasks are decomposed by observable functionality and normally map one-to-one to independently publishable pull requests. A typical slice targets 300–700 changed production-code lines, while tests, documentation, configuration, generated files, and binaries are reported separately. The limit is advisory: an indivisible change remains one task when splitting would make an intermediate state unbuildable, untestable, unsafe to deploy, or impossible to roll back.

## Skills

- `workflow` runs repository mutations through the durable Codex-led cycle.
- `review` obtains and verifies an independent read-only review.
- `setup` checks the Claude subscription runtime without model inference.
- `delivery-writing` prepares Jira tasks, testing recommendations, PR descriptions, and review comments. Drafting alone never publishes them; a separate publication task may execute only its shown package after `публикуем`.
- `epic-workflow` decomposes an epic into durable v1 runs and can perform a blind replay of a closed epic before comparing with historical Jira, PR, and Git evidence.

## Install on another computer

Install from the public repository using the native Codex marketplace flow:

```bash
codex plugin marketplace add 20grizz03/codex-agent-harness --ref main
codex plugin add codex-agent-harness@agent-harness-local
```

Claude inference is subscription-only. Authenticate interactively when needed:

```bash
claude auth login
```

Start a new Codex task after installation so the skills and MCP tools are discovered from the installed snapshot. Ask Codex to check Agent Harness setup; the check does not invoke a model or install anything.

See [`plugins/codex-agent-harness/docs/dependencies.md`](plugins/codex-agent-harness/docs/dependencies.md) for the complete runtime, optional OpenSpec setup, connector, and development-tool inventory bundled with the plugin.

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
<git-common-dir>/codex-agent-harness/campaigns/<campaign-id>/
```

Operational state never dirties the worktree. Run contracts stay under the worktree's absolute Git directory; shared campaign state and its approved OpenSpec snapshot live under the common Git directory with mode `0600`. The canonical `/openspec` working folder stays in the project but is ignored by Git by default. Versioned OpenSpec remains an explicit team choice.

Repository owners can add path-based risk escalation and deterministic checks through [`docs/configuration.md`](docs/configuration.md). The configuration can only add gates or raise risk; it cannot weaken the contract frozen at run creation.

Epic campaign state and the replay boundary are documented in [`docs/epic-campaigns.md`](docs/epic-campaigns.md).
