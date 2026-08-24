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

The harness always prepends `git-diff-check`. It then adds the checks frozen from repository guidance at run creation and the checks from every matching rule. Check names are stable identifiers. Reusing a name with a different argv array or timeout is a configuration error rather than an override.

Codex executes returned commands through its ordinary sandbox after checking project configuration for data egress. The MCP server only plans commands and records bounded results.
