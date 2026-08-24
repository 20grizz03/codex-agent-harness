# Connector origin and exclusions

The Claude process adapter is a clean extraction of invariants validated in the earlier `codex-claude-synergy` experiment:

- resolve the local Claude Code executable and check public auth status without invoking a model;
- block `ANTHROPIC_API_KEY`, custom Anthropic endpoints, Bedrock, Vertex, and Foundry routing;
- require safe mode, sandboxing, no session persistence, no Chrome, no dynamic prompt sections, and an empty strict MCP configuration;
- stream only allowlisted progress, discard stderr content and raw JSONL, support cancellation, and record actual model/usage metadata;
- keep Codex as lead and expose model lifecycle through a visible native tracking subagent.

The new repository intentionally excludes capacity policies, `claude_first` routing, subscription load balancing, provider comparison, blind A/B benchmarks, automatic fallback, separate architect/tester products, direct compatibility launchers, trackers, schedulers, and remote daemons.
