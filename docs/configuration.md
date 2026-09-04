# Repository policy

Add `.codex/agent-harness.json` to a target repository when checks or risk depend on changed paths. Version 1 has this shape:

```json
{
  "version": 1,
  "rules": [
    {
      "paths": ["src/**/*.py", "src/*.py"],
      "risk": "high",
      "checks": [
        {
          "name": "unit",
          "argv": ["python3", "-m", "unittest"],
          "timeout_seconds": 600
        }
      ]
    }
  ]
}
```

`paths` contains repository-relative glob patterns. A rule matches when any changed path matches any pattern. `risk` is optional and may be `low`, `medium`, or `high`; matching rules can only raise the contract risk. `checks` is optional and contains argv arrays, never shell command strings.

The harness always prepends `git-diff-check` with argv `git diff --check`. When Codex records its success, the server separately validates the full result from the frozen base, including committed, staged, unstaged, and untracked content without changing the index. The harness then adds checks inherited from a campaign task, checks frozen from repository guidance at run creation, and checks from every matching rule. A linked run may add a check but cannot remove an inherited one. Check names are stable identifiers; reusing a name with different arguments or timeout is a configuration error.

Codex executes returned commands through its ordinary sandbox after checking project configuration for data egress. The MCP server only plans commands and records bounded results. A small edit to this configuration is still a behavioral change and is not eligible for the concise non-executable-edit path.
