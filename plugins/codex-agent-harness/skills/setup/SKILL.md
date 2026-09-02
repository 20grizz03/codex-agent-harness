---
name: setup
description: Check and troubleshoot the local Agent Harness runtime and external dependencies without invoking a model. Use for installation on another computer, Claude authentication and billing guards, OpenSpec, required CLI tools, or Jira, GitHub Enterprise, and Telegram MCP readiness.
---

# Agent Harness Setup

Call `agent-harness.check_runtime` directly. This tool must never invoke a model.

Report the resolved Claude path and version, public authentication fields, required-flag support, configured model, and names of active API/provider-billing environment variables. Never report their values.

Readiness requires Claude Code, all safety flags, a logged-in account, and no active `ANTHROPIC_API_KEY`, custom Anthropic base URL, Bedrock, Vertex, or Foundry routing. There is no bypass for API billing. If logged out, ask the user to run `claude auth login` interactively; do not run that login command on their behalf.

When the user asks to prepare or audit another computer, also perform these read-only checks:

1. Resolve `codex`, `git`, `python3`, `claude`, `node`, and `openspec` from `PATH` and report their paths and public versions. OpenSpec is optional; when present, require Node.js 20.19.0 or newer and invoke its diagnostic commands through `env OPENSPEC_TELEMETRY=0`.
2. Use `codex plugin list --json` to check only whether `codex-agent-harness@agent-harness-local` is installed and enabled. Summarize those fields; do not reproduce the raw response.
3. Inspect active skill names only for project-specific profiles required by the target repository; do not copy or traverse the user's personal skill directories.
4. Inspect the active MCP tool names. If needed, use plain `codex mcp list` and report only server names and enabled state. Never use or reproduce JSON MCP configuration because server definitions may contain inline credentials.
5. If OpenSpec is present, use `env OPENSPEC_TELEMETRY=0 openspec schema which agent-harness` to report whether the bundled personal schema has been installed. Do not inspect change contents during setup.
6. Classify missing components against [`docs/dependencies.md`](../../docs/dependencies.md): core runtime blocks Agent Harness readiness; missing OpenSpec blocks large, high-risk, ambiguous, multi-task, or behaviorally complex single-task implementation, but not a narrow unambiguous direct task; missing Jira, GitHub Enterprise, Telegram, or required project-specific components blocks only that integration profile; missing `gh` or `gitleaks` is an optional-tool notice. Treat GitHub Enterprise MCP as the primary GitHub interface and `gh` only as a diagnostic or narrow fallback.

For missing OpenSpec, print the installation and schema-copy commands from the dependency document without running them. For a missing external connector or personal skill, point to its dependency entry instead of inventing configuration. Never install software, initialize OpenSpec in a repository, authenticate an account, copy a session, or modify Codex configuration without a separate explicit request.
