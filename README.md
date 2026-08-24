# Codex Agent Harness

`codex-agent-harness` is a local Codex plugin for durable repository-changing tasks. Codex records an immutable task contract, runs deterministic checks through its normal sandbox, obtains one fresh independent Claude review, verifies the findings, and completes only against evidence tied to the current diff.

The plugin deliberately does not balance subscriptions, compare providers, schedule tracker work, run a daemon, or fall back to another model.

## Install locally

```bash
codex plugin marketplace add <path-to-codex-agent-harness>
codex plugin add codex-agent-harness@agent-harness-local
```

Start a new Codex task after installation so the skills and MCP tools are discovered from the installed snapshot.

Claude inference is subscription-only. Authenticate interactively when needed:

```bash
claude auth login
```

`check_runtime` reports readiness without invoking a model and refuses inference when API/provider-billing environment variables are active.

## Development

The plugin is under `plugins/codex-agent-harness`. It uses only the Python standard library and communicates with Codex over stdio MCP. Run the commands in `AGENTS.md` before a local commit.

Runtime state is stored per target worktree under:

```text
<absolute-git-dir>/codex-agent-harness/runs/<run-id>/
```

The worktree remains clean: `contract.json`, `state.json`, `events.jsonl`, and `review.json` live in Git metadata with mode `0600`.

Repository owners can add path-based risk escalation and deterministic checks through [`docs/configuration.md`](docs/configuration.md). The configuration can only add gates or raise risk; it cannot weaken the contract frozen at run creation.
