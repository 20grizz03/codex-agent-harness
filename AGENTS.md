# Codex Agent Harness development guidance

This repository contains one local Codex marketplace and the `codex-agent-harness` plugin. Keep the implementation Python-standard-library only.

Codex is the lead and default writer. Claude is an independent critic unless the current user explicitly selects Claude as writer. The v2.1 epic workflow may use existing Jira and GitHub Enterprise connectors, but this repository must not embed tracker clients, credentials, automatic external writes, provider balancing, generic model fallback, comparative provider benchmarks, daemon scheduling, or remote orchestration.

The only review fallback is one fresh read-only native Codex reviewer after the Claude critic returns the sanitized `anthropic_limit` failure or a campaign cooldown emits the equivalent no-inference terminal. Persist it as `codex_fallback`; never retry the same Claude stage or use fallback for timeouts, authentication failures, process errors, invalid output, or cancellation. A later campaign task may make one recovery probe after the persisted cooldown expires.

Operational state belongs under each target repository's absolute Git directory, never in its worktree. Persist only allowlisted structured data; never persist raw Claude JSONL, prompts, tool arguments, stderr, credentials, or environment values.

Run before committing:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" plugins/codex-agent-harness/skills/workflow
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" plugins/codex-agent-harness/skills/review
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" plugins/codex-agent-harness/skills/setup
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" plugins/codex-agent-harness/skills/delivery-writing
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" plugins/codex-agent-harness/skills/epic-workflow
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/plugin-creator/scripts/validate_plugin.py" plugins/codex-agent-harness
git diff --check
```

Do not invoke a real model in automated tests. Use a fake Claude executable. Never add AI authorship trailers to commits.
