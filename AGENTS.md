# Codex Agent Harness development guidance

This repository contains one local Codex marketplace and the `codex-agent-harness` plugin. Keep the implementation Python-standard-library only.

Codex is the lead and default writer. Prepared campaign tasks default to native `gpt-5.6-sol` execution at `high` reasoning effort; the lead passes both values explicitly when spawning the executor. Planning and a recorded escalation may use the task's stronger `escalation_model`. Claude is an independent critic unless the current user explicitly selects Claude as writer. The epic workflow may use existing Jira and GitHub Enterprise connectors, but this repository must not embed tracker clients, credentials, automatic external writes, provider balancing, generic model fallback, comparative provider benchmarks, daemon scheduling, or remote orchestration.

The only review fallback is one fresh read-only native Codex reviewer after the Claude critic returns the sanitized `anthropic_limit` failure or a campaign cooldown emits the equivalent no-inference terminal. Persist it as `codex_fallback`. A run may explicitly request a bounded new critic stage after `transient_timeout` or `transient_process_failure` while its frozen retry budget remains; the request refers to the failed stage and is idempotent. Never retry or use fallback for authentication, billing or safety failures, invalid output, cancellation, or an unapproved model change. A later campaign task may make one recovery probe after the persisted cooldown expires.

Run state belongs under the target worktree's absolute Git directory. Campaign state and its approved local OpenSpec snapshot belong under the repository's common Git directory so isolated worktrees share them. Neither may enter the worktree. Persist only allowlisted structured data; never persist raw Claude JSONL, prompts, tool arguments, stderr, credentials, or environment values. Existing authorization persists across turns and covers reversible local edits, checks, corrections, approved commits, and unchanged retries; do not ask for it again. Ask only for a product or scope change, a user-owned-file conflict whose intent cannot be resolved safely, or an external or irreversible action that has not already been authorized.

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
