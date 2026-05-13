# Overwatch

Overwatch watches GitHub pull requests, records CI state in SQLite, and invokes a coding agent when CI fails.

## Usage

```sh
overwatch https://github.com/OWNER/REPO/pull/123 --provider codex --model gpt-5.5
overwatch --run-once
overwatch --worker --interval 300
```

Set `GITHUB_TOKEN` for private repositories or higher API limits.

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
