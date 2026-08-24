# Independent review contract

The critic receives the immutable task contract, base SHA, current diff fingerprint, changed paths, check evidence, and risk areas. It does not receive a pasted diff and must inspect the repository and Git directly.

The `critic` profile is read-only: Claude runs with safe mode, plan permissions, no session persistence, no Chrome, no dynamic system-prompt sections, an empty strict MCP configuration, and only `Read`, `Glob`, `Grep`, and `Bash`. `Edit`, `Write`, and `NotebookEdit` are explicitly denied. Bash is limited to read-only repository inspection; project checks remain the Codex lead's responsibility.

The structured result contains:

- `verdict`: `pass`, `changes_requested`, or `blocked`;
- `findings`: actionable P0-P3 findings with stable ID, file, optional line, title, impact, evidence, and concrete fix;
- `residual_risks`: validation gaps that are not proven defects;
- `blocking_question`: at most one question that genuinely prevents a safe conclusion.

Ignore style preferences without concrete impact. Do not report a finding based only on a failing check already present in the supplied evidence. P0 and P1 require a reproducible path or direct code evidence.

Codex independently traces each finding through surrounding production code and tests. Merge duplicates, reject unsupported claims with contrary evidence, and keep `unverified` findings unresolved. A passing Claude verdict does not override a failed deterministic check.

The only degraded reviewer is a fresh read-only native Codex subagent after the persisted Claude stage reports `failure_kind: anthropic_limit`. It follows this same contract, reads Git directly, and is stored with origin `codex_fallback`. Do not reinterpret a generic provider error as a limit and do not hide the fallback origin from the user.
