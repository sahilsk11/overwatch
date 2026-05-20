# Overwatch

Overwatch watches GitHub pull requests, records CI state in SQLite, and invokes a coding agent when CI fails.

## Usage

```sh
overwatch https://github.com/OWNER/REPO/pull/123
overwatch https://github.com/OWNER/REPO/pull/123 --autofix --merge-on-bot-approval --turns 3
overwatch https://github.com/OWNER/REPO/pull/123 --context-file ./overwatch-context.md --session-strategy context-summary
overwatch --pause 1
overwatch --resume 1
overwatch --stop 1
overwatch --run-once
overwatch --worker
```

Set `GITHUB_TOKEN` for private repositories or higher API limits.

`--autofix` runs the configured coding agent when CI fails or unresolved review comments exist.
`--context` and `--context-file` store a durable context summary with the watched PR. The API accepts `context` or `context_summary` on `POST /api/prs`.
`--session-strategy` controls how much durable context Overwatch includes in provider prompts. It defaults to `context-summary`, matching the Phase 4 recommendation to preserve product context while still spawning fresh provider processes. Use `fresh` for the most predictable isolated prompt; in fresh mode Overwatch omits the durable context summary and prior provider turns. `attached-session` is an advanced metadata mode that requires `--session-id` and warns that provider session resume is not implemented yet.
`--turns` sets the provider turn budget for a watched PR. It defaults to `3` and accepts values from `1` through `10`. Each provider launch consumes one turn. When more automated work is needed after the budget is used, Overwatch marks the PR `needs-human`.
`--pause`, `--resume`, and `--stop` accept a watch database ID or exact PR URL. Paused and stopped watches are skipped by the worker before another provider turn starts. Paused watches remain visible in the active list; stopped watches are treated as terminal and show in `--all` or the API `done` bucket.
`--merge-on-bot-approval` keeps watching the PR and, once CI is green and there are no unresolved review comments, merges it if the latest supported bot review/comment approves it. Today that supports Codex-authored text saying Codex did not find any major issues. If auto-fix completes while merge-on-bot-approval is enabled, Overwatch asks Codex to re-review.

Overwatch defaults to the `codex` provider with model `gpt-5.5`, and the worker polls every 60 seconds unless `--interval` is set.

Provider prompts explicitly tell agents to treat review comments as hypotheses, preserve the no-CI merge policy, explain invalid comments instead of making policy-breaking changes, and stop with a summary of blockers evaluated, changes made, tests run, commit/push status, and remaining human action. In `context-summary` and `attached-session` modes, prompts also include the stored durable context summary and prior provider turns.

## API and visibility

`GET /api/prs` and `GET /api/prs/{id}` include operational fields such as `worker_status`, `turns_used`, `max_turns`, active attempt ID/start/elapsed time, last attempt status, last provider command/output, and last error. `GET /api/prs/{id}/events` includes CI history and resolution attempts with turn number, command, truncated provider output, and error details.

Use `POST /api/prs/{id}/pause`, `POST /api/prs/{id}/resume`, and `POST /api/prs/{id}/stop` for API controls.

## Development

This project uses [uv](https://docs.astral.sh/uv/) for environment and dependency management.

```sh
uv sync
uv run pytest
uv run ruff check .
```

## Providers

Overwatch uses a small Provider registry inspired by Friday's provider abstraction. The initial adapters invoke local CLIs:

- `opencode`: `opencode run`
- `codex`: `codex exec --dangerously-bypass-approvals-and-sandbox --sandbox danger-full-access --color never`
- `claude-code`: `claude --print`

Override commands with `OVERWATCH_OPENCODE_CMD`, `OVERWATCH_CODEX_CMD`, or `OVERWATCH_CLAUDE_CODE_CMD`.

`attached-session` records a user-provided session ID and treats that session as owned by the watch until the watch completes or is stopped. Current providers do not resume that session, so this mode is not the default and should only be used when you are deliberately tracking future session handoff behavior.

## Storage

The default database is `~/.overwatch/overwatch.sqlite3`. It stores watched PRs, durable context summaries, session strategy/session IDs, CI check history, watch turns, and resolution attempts. Attempts are capped per head SHA, and each watched PR has a provider turn budget to avoid infinite retry loops. Provider command output is truncated before storage so diagnostics are available without unbounded logs.
