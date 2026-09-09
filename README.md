# Codex Agent Harness

`codex-agent-harness` is a local Codex plugin for durable repository-changing tasks and multi-task epic campaigns. Codex records immutable contracts, runs deterministic checks through its normal sandbox, obtains independent review for the current result, and completes only against evidence tied to the full diff from the frozen base.

The plugin includes delivery-writing conventions: compact Jira tasks, post-implementation testing recommendations, one- or two-sentence PR descriptions, natural Russian technical prose, and concise colleague-facing review comments. Drafting text does not authorize changing Jira or publishing to GitHub. Large ideas, high-risk or ambiguous changes, live multi-task epics, and behaviorally complex single tasks keep their approved decomposition in OpenSpec; Agent Harness remains the execution and evidence layer.

Prepared tasks persist their native executor settings, acceptance criteria, constraints, required checks, versioned contract references, correction budget, and critic-retry budget. The default executor is `gpt-5.6-sol` with `high` reasoning effort and no default escalation model; an epic plan may explicitly record `gpt-6-astra` for planning or justified escalation. The Codex lead passes the executor settings to the native task. The MCP server never launches or attests Sol itself. Claude remains the fresh read-only critic.

Campaigns may run several dependency-free tasks in parallel waves. When a repository has two or more implementation tasks, each of its waves ends in a checked integration commit; dependent work starts from that verified base. Local execution dependencies are separate from `parallel`, `stacked`, and `after_merge` publication order. Every changed result keeps current checks and review history. A bounded explicit retry is available only for classified transient critic timeouts or process failures. A confirmed Anthropic usage limit still uses the campaign cooldown, later recovery probe, and visibly marked `codex_fallback`; it is never treated as a generic provider switch.

Technical design must be sufficiently resolved before implementation tasks are proposed: affected components, contracts, failure and recovery behavior, rollout order, and publication and rollback boundaries are recorded first. Every requirement maps to at least one task and every task maps to a verification scenario. Referenced contracts name a version and are attached or resolvable; their actual availability is checked before implementation. The lead may settle ordinary technical choices inside these boundaries. Uncertainty that changes product behavior, scope, or task boundaries becomes an analysis task or a user question. A typical slice targets 300–700 changed production-code lines, while tests, documentation, configuration, generated files, and binaries are reported separately. The limit remains advisory.

An obvious low-risk edit to non-executable text may use a concise direct path without a campaign or durable run. Instruction, `AGENTS.md`, configuration, security, and behavior changes are not eligible merely because their diff is small. Repository-mandated checks still run. Once the user approves a concrete plan, reversible local work proceeds without repeated confirmations; external and irreversible actions remain separately authorized.

An active publication task may also apply a [small follow-up](plugins/codex-agent-harness/skills/delivery-writing/references/publication-context.md#мелкие-доработки) to an already verified candidate, including a narrow low-risk code fix, without a new run or automatic tests, manual smoke, or independent review. It preserves the verified baseline, records the entire follow-up delta as unverified, and shows a new publication package for approval. This does not complete an unfinished run, override explicit repository checks, or provide a verified dependency for another campaign wave. Broader or risk-sensitive fixes return to the standard workflow.

## Skills

- `workflow` runs repository mutations through the durable Codex-led cycle.
- `review` obtains and verifies an independent read-only review.
- `setup` checks the Claude subscription runtime without model inference.
- `delivery-writing` prepares Jira tasks, testing recommendations, PR descriptions, and review comments. Drafting alone never publishes them; the current user-visible task may execute only the verified, shown package and only after an explicit instruction naming the action.
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

Repository owners can add path-based risk escalation and deterministic checks through [`docs/configuration.md`](docs/configuration.md). Campaign-linked runs inherit task criteria, constraints, non-goals, forbidden actions, checks, contract references, and execution settings. Callers may add criteria, restrictions, checks, and references but cannot replace the goal, override a same-name check or frozen setting, or weaken inherited requirements.

Epic campaign state and the replay boundary are documented in [`docs/epic-campaigns.md`](docs/epic-campaigns.md).
