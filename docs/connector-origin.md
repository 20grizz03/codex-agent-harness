# Connector origin and exclusions

The Claude process adapter is a clean extraction of invariants validated in the earlier `codex-claude-synergy` experiment:

- resolve the local Claude Code executable and check public auth status without invoking a model;
- block `ANTHROPIC_API_KEY`, custom Anthropic endpoints, Bedrock, Vertex, and Foundry routing;
- require safe mode, sandboxing, no session persistence, no Chrome, no dynamic prompt sections, and an empty strict MCP configuration;
- stream only allowlisted progress, discard stderr content and raw JSONL, support cancellation, and record actual model/usage metadata;
- keep Codex as lead and prefer a visible native tracking subagent for model lifecycle; if its plugin tools are unavailable, the Codex instance that owns the run uses the same configured MCP server without starting another process.

The repository intentionally excludes capacity policies, `claude_first` routing, subscription load balancing, provider comparison, blind provider A/B benchmarks, automatic fallback, separate architect/tester products, direct compatibility launchers, embedded tracker clients, schedulers, and remote daemons.

The v2.1 `epic-workflow` skill may consume Jira and GitHub Enterprise MCP tools already configured in Codex. Those connectors remain outside the plugin process: the harness stores only sanitized contracts, references, intervention summaries, and comparison scores. It never stores connector credentials or raw responses, and local completion never authorizes an external write.
