---
name: setup
description: Check and troubleshoot the local Claude Code runtime used by Agent Harness without invoking a model. Use for installation, authentication, subscription billing guards, required CLI flags, or Agent Harness readiness problems.
---

# Agent Harness Setup

Call `agent-harness.check_runtime` directly. This tool must never invoke a model.

Report the resolved Claude path and version, public authentication fields, required-flag support, configured model, and names of active API/provider-billing environment variables. Never report their values.

Readiness requires Claude Code, all safety flags, a logged-in account, and no active `ANTHROPIC_API_KEY`, custom Anthropic base URL, Bedrock, Vertex, or Foundry routing. There is no bypass for API billing. If logged out, ask the user to run `claude auth login` interactively; do not run that login command on their behalf.
