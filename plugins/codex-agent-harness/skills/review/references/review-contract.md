# Independent review contract

The critic receives the immutable task contract, base SHA, current full-result fingerprint, changed paths, check evidence, review cycle, and risk areas. It does not receive a pasted diff and must inspect the repository and Git directly.

The `critic` profile is read-only: Claude runs with safe mode, plan permissions, no session persistence, no Chrome, no dynamic system-prompt sections, an empty strict MCP configuration, and only `Read`, `Glob`, `Grep`, and `Bash`. `Edit`, `Write`, and `NotebookEdit` are explicitly denied. Bash is limited to read-only repository inspection; project checks remain the Codex lead's responsibility.

Classify findings before choosing the verdict. The structured result contains:

- `verdict`: `pass`, `changes_requested`, or `blocked`;
- `findings`: actionable P0-P3 findings with stable ID, file, optional line, title, impact, evidence, and concrete fix;
- `residual_risks`: validation gaps that are not proven defects;
- `blocking_question`: at most one question that genuinely prevents a safe conclusion.

`pass` requires `findings: []`; any actionable P0-P3 finding requires `changes_requested`. `blocked` requires a non-empty blocking question and retains any confirmed findings. Keep validation gaps in `residual_risks`, but never move a confirmed defect there to obtain `pass`. Runtime normalization may reject or conservatively repair a contradictory result; the reviewer should still produce a consistent result directly.

Ignore style preferences without concrete impact. Do not report a finding based only on a failing check already present in the supplied evidence. P0 and P1 require a reproducible path or direct code evidence.

Codex independently traces each finding through surrounding production code and tests. Merge duplicates, reject unsupported claims with contrary evidence, and keep `unverified` findings unresolved. A passing Claude verdict does not override a failed deterministic check.

One explicit retry may create a new critic stage only after `transient_timeout` or `transient_process_failure` and only within the immutable one-retry budget. It names the failed stage and is idempotent. The only degraded reviewer is a fresh read-only native Codex subagent after `failure_kind: anthropic_limit`; it follows this contract and is stored with origin `codex_fallback`. Never reinterpret another provider error as a limit.

Any correction changes the fingerprint. Its checks and independent review must run again as a new cycle; prior structured results stay in history but do not complete the corrected result.
