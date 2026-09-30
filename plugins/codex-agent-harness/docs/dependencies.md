# Dependencies

Agent Harness bundles only the components that it owns. External skills and account-backed connectors stay in their original projects so they can be updated and authenticated independently.

## Bundled

The `codex-agent-harness` plugin contains:

- the `workflow`, `review`, `setup`, `delivery-writing`, and `epic-workflow` skills;
- the `agent-harness` stdio MCP server;
- a Python-standard-library runtime with no package installation step.

## Core runtime

These components are required for a complete implementation run:

| Component | Purpose | Setup |
| --- | --- | --- |
| Codex with plugin support | Runs the workflow and bundled MCP server | Install Codex, then follow the repository README |
| Native `gpt-6-sol` availability | Executes prepared implementation tasks at the stored reasoning effort | Confirm the model is available on the target Codex host before the task wave |
| Git | Finds the repository, computes diff fingerprints, and isolates dirty worktrees | Install with the operating system developer tools |
| Python 3 | Runs the bundled MCP server and tests | Make `python3` available on `PATH` |
| Claude Code CLI 2.1.280+ with `claude-opus-5-5` access | Provides the independent read-only critic at `high` effort by default | Update Claude Code; run `claude auth login` interactively only when public auth status confirms logout |

Claude must use first-party subscription OAuth. `check_runtime` rejects API keys, custom Anthropic endpoints, Bedrock, Vertex, and Foundry routing and never invokes a model itself. Native Codex model selection is separate planning metadata; Agent Harness does not launch or attest the selected Codex model.

### Диагностика Claude Code

Для Opus 5.5 нужен Claude Code 2.1.280 или новее; для закреплённого Opus 5 — 2.1.219 или новее. Авторизация и наличие флагов сами по себе не подтверждают совместимость. `check_runtime` возвращает `cli_model_incompatible` до создания этапа и расхода попытки; неизвестный формат версии — `cli_version_unknown`. Версии сверены с [официальной документацией](https://code.claude.com/docs/en/errors#thinking-type-enabled-is-not-supported-for-this-model). Обновление CLI выполняется отдельно, не автоматически из Harness.

При отказе API сохраняются числовые `telemetry.exit_code`, `telemetry.provider_error.http_status` и фиксированный код причины, без исходного сообщения. HTTP 400 и несовместимые параметры thinking получают `request_configuration`; они не разрешают повтор или переход к другой модели. Ошибка может прийти в сообщении `assistant` или в результате с `subtype: success` и `is_error: true` — это не успешное ревью.

`check_runtime` запускает только `--version`, `auth status --json` и `--help` из отдельного временного каталога. Удаление рабочего каталога сервера не должно мешать этой проверке. Каталог самого ревью остаётся рабочим репозиторием; права критика не меняются.

В неуспешном результате `error_code` различает отсутствие CLI (`cli_not_found`), ошибку запуска (`cli_spawn_failed`), тайм-аут (`cli_timeout`), ошибку команды (`cli_command_failed`), некорректный ответ авторизации (`auth_invalid_response`) и явное отсутствие авторизации (`authentication_required`). Для диагностической команды возвращаются `failed_check` и, при наличии, `exit_code`, без сырых stdout/stderr. Ограничения модели, биллинга и обязательных флагов сохраняются.

Только корректный ответ с логическим `loggedIn: false` означает, что CLI не видит авторизацию в текущей среде. Неудачный запуск или некорректный JSON не означают выход из аккаунта: `auth` остаётся неизвестным. Если терминал и MCP расходятся, сравни публичный статус авторизации в среде запуска сервера и обычном терминале до предложения `claude auth login`. Не переноси токены и не снимай ограничения песочницы ради успешной проверки. Неудачная диагностика до вызова модели не расходует попытку критика и не завершает прогон; после устранения причины можно повторить запуск этапа в том же прогоне.

## Specification-backed decomposition

Large ideas, high-risk or ambiguous changes, multi-task epics, and behaviorally complex single tasks need an approved specification. The default native format uses only local Markdown: `.agent-harness/specs/<change-id>/spec.md` for the common contract and `tasks/<task-id>.md` for every campaign task. A standalone run may use only `spec.md`. Keep `/.agent-harness/specs/` in the target repository's local `.git/info/exclude`; Agent Harness freezes the approved files into private Git metadata. No Node.js, OpenSpec CLI, schema install, `openspec init`, or mandatory OpenSpec syntax is needed. The [native specification guide](../skills/epic-workflow/references/native-spec.md) covers preparation and approval. A narrow, unambiguous single task can use the direct workflow without a spec.

### Explicit or legacy OpenSpec mode

[OpenSpec](https://github.com/Fission-AI/OpenSpec) remains available when the user explicitly selects it or an existing campaign has `kind: openspec`. It is not a dependency of native specs and existing campaigns are not rewritten automatically.

OpenSpec currently requires Node.js 20.19.0 or newer. Install the CLI separately:

```bash
npm install -g @fission-ai/openspec@latest
```

From a checkout of this repository, copy the bundled concise schema into the user-level OpenSpec schema directory and validate it:

```bash
mkdir -p ~/.local/share/openspec/schemas
cp -R plugins/codex-agent-harness/skills/epic-workflow/assets/openspec-schema/agent-harness ~/.local/share/openspec/schemas/
env OPENSPEC_TELEMETRY=0 openspec schema validate agent-harness
```

For local OpenSpec mode, keep `/openspec` in the target project but add `/openspec/` to the repository's local `.git/info/exclude` before creating files. Agent Harness requires the directory to be ignored and snapshots the approved change in private Git metadata. This local mode does not require `openspec init`.

Initialize a target repository only when its owners explicitly want versioned specs:

```bash
env OPENSPEC_TELEMETRY=0 openspec init
```

The setup audit never runs installation, schema copy, or `openspec init`. Repeat the copy command after a plugin update when using this legacy mode so existing user-level schema files receive new templates and instructions. Agent Harness disables OpenSpec telemetry in every command it invokes. The custom OpenSpec schema retains its own headings and syntax; they do not apply to native Markdown. Both modes require technical readiness, requirement-to-task-to-scenario coverage, pinned contract revisions and availability checks, and an advisory 300–700 production-line target per implementation slice.

## Scenario integrations

These are required only when the task uses the corresponding system:

| Component | Expected Codex name | Used for |
| --- | --- | --- |
| Jira MCP | `jira` | Reading an epic and, after explicit approval, changing Jira |
| GitHub Enterprise MCP | `github-enterprise` | Primary interface for pull requests, reviews, checks, and repository metadata on the repository's configured GitHub host |
| Telegram MCP | `telegram` or a profile-specific name | Reading Telegram sources and explicitly approved channel operations |

Connector credentials, OAuth data, Telegram sessions, and organization-specific launchers must remain outside this repository. Use a separate session data directory and MCP entry for each Telegram account, following the selected connector's documentation. Enabling Telegram write tools does not authorize a publication; every external write still requires explicit approval.

Do not paste complete MCP configuration into diagnostics or logs. Server definitions may contain inline credentials; report only the connector name and whether it is enabled.

## Project-specific profiles

Project-specific implementation and review skills remain external. Use them when the target repository requires them; they are not dependencies of generic Agent Harness runs. Keep their configuration and distribution separate from this plugin.

## Optional command-line tools

- `gh` is a diagnostic or narrow fallback when GitHub Enterprise MCP is unavailable or lacks a required operation. It is not the normal GitHub interface. Enterprise calls must specify `--hostname <repository-host>` using the host from the target repository; fetch, commit, and push continue to use local Git and the repository's SSH remote.
- `gitleaks` is development-only and is used for release-time secret scanning. It is not part of the Agent Harness runtime.

## Setup boundary

The `setup` skill audits this inventory and prints remediation commands. It never installs dependencies, authenticates accounts, copies sessions, changes Codex configuration, or invokes a model. Start a new Codex task after installing or updating a plugin so the installed snapshot is discovered.
