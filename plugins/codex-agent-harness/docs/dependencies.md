# Dependencies

Agent Harness bundles only the components that it owns. External skills and account-backed connectors stay in their original projects so they can be updated and authenticated independently.

## Bundled

The `codex-agent-harness` plugin contains:

- the `workflow`, `review`, `setup`, `delivery-writing`, and `epic-workflow` skills;
- the `agent-harness` stdio MCP server;
- a Python-standard-library runtime with no package installation step.

## Core runtime

These components are required for a complete implementation run:

| Component | Purpose | Setup |
| --- | --- | --- |
| Codex with plugin support | Runs the workflow and bundled MCP server | Install Codex, then follow the repository README |
| Git | Finds the repository, computes diff fingerprints, and isolates dirty worktrees | Install with the operating system developer tools |
| Python 3 | Runs the bundled MCP server and tests | Make `python3` available on `PATH` |
| Claude Code CLI | Provides the independent read-only critic | Install Claude Code and run `claude auth login` interactively |

Claude must use first-party subscription OAuth. `check_runtime` rejects API keys, custom Anthropic endpoints, Bedrock, Vertex, and Foundry routing and never invokes a model itself.

## Default companion

[Ponytail](https://github.com/DietrichGebert/ponytail) is installed separately and is expected by default for repository-changing tasks:

```bash
codex plugin marketplace add https://github.com/DietrichGebert/ponytail.git
codex plugin add ponytail@ponytail
```

Its absence does not break the Agent Harness MCP server, but the setup audit reports that the default minimal-implementation profile is incomplete.

## Scenario integrations

These are required only when the task uses the corresponding system:

| Component | Expected Codex name | Used for |
| --- | --- | --- |
| Jira MCP | `jira` | Reading an epic and, after explicit approval, changing Jira |
| GitHub Enterprise MCP | `github-enterprise` | Primary interface for pull requests, reviews, checks, and repository metadata on the repository's configured GitHub host |
| Telegram MCP | `telegram` or a profile-specific name | Reading Telegram sources and explicitly approved channel operations |

Connector credentials, OAuth data, Telegram sessions, and organization-specific launchers must remain outside this repository. Use a separate session data directory and MCP entry for each Telegram account, following the selected connector's documentation. Enabling Telegram write tools does not authorize a publication; every external write still requires explicit approval.

Do not paste complete MCP configuration into diagnostics or logs. Server definitions may contain inline credentials; report only the connector name and whether it is enabled.

## Project-specific profiles

Project-specific implementation and review skills remain external. Use them when the target repository requires them; they are not dependencies of generic Agent Harness runs. Keep their configuration and distribution separate from this plugin.

## Optional command-line tools

- `gh` is a diagnostic or narrow fallback when GitHub Enterprise MCP is unavailable or lacks a required operation. It is not the normal GitHub interface. Enterprise calls must specify `--hostname <repository-host>` using the host from the target repository; fetch, commit, and push continue to use local Git and the repository's SSH remote.
- `gitleaks` is development-only and is used for release-time secret scanning. It is not part of the Agent Harness runtime.

## Setup boundary

The `setup` skill audits this inventory and prints remediation commands. It never installs dependencies, authenticates accounts, copies sessions, changes Codex configuration, or invokes a model. Start a new Codex task after installing or updating a plugin so the installed snapshot is discovered.
