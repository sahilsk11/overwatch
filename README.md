# Overwatch

Overwatch watches GitHub pull requests, records CI state in SQLite, and invokes a coding agent when CI fails.

## Usage

```sh
overwatch https://github.com/OWNER/REPO/pull/123 --provider codex --model gpt-5.5
overwatch https://github.com/OWNER/REPO/pull/123 --merge-on-bot-approval
overwatch --run-once
overwatch --worker --interval 300
```

Set `GITHUB_TOKEN` for private repositories or higher API limits.

`--merge-on-bot-approval` keeps watching the PR and, once CI is green, merges it if the latest supported bot review/comment approves it. Today that supports Codex-authored text saying Codex did not find any major issues. If the bot leaves actionable feedback instead, Overwatch leaves the PR open.

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
- `codex`: `codex exec`
- `claude-code`: `claude --print`

Override commands with `OVERWATCH_OPENCODE_CMD`, `OVERWATCH_CODEX_CMD`, or `OVERWATCH_CLAUDE_CODE_CMD`.

## Storage

The default database is `~/.overwatch/overwatch.sqlite3`. It stores watched PRs, CI check history, and resolution attempts. Attempts are capped per head SHA to avoid infinite retry loops.
