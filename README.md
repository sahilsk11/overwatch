# Overwatch

Overwatch stores watched GitHub pull requests in SQLite, shows them in the existing UI, and can wake a supervisor Codex agent to handle the actual PR work.

The important bit: the scheduled command no longer tries to classify CI, parse reviews, or enforce merge policy in Python. It starts one supervisor agent with the active watch list and tells that agent to use `gh` and spawn child Codex processes per PR.

## Usage

```sh
overwatch https://github.com/OWNER/REPO/pull/123 --context-file ./notes.md --max-turns 3
overwatch list
overwatch --pause 1
overwatch --resume 1
overwatch --stop 1
overwatch --tick
overwatch --serve
```

Adding a watch requires an active local worker heartbeat. If no worker is
running, the CLI refuses the watch instead of silently storing work that no
process will pick up.

Run the worker continuously:

```sh
overwatch --worker
```

`--tick` loads unresolved watches from `~/.overwatch/overwatch.sqlite3`, starts one supervisor agent, and gives it a prompt that says:

- inspect the watch list
- use `gh` to inspect each PR
- respect `autofix` and `merge_on_bot_approval`
- request `@codex review` for the current head when merge-on-bot-approval is enabled and no Codex review is already present
- spawn a separate child Codex process for each PR that needs repair
- have each child make one focused repair attempt and stop

## UI

`overwatch --serve` keeps the FastAPI backend and frontend available as a read-only view over watches and recorded activity. Use the CLI to add, pause, resume, or stop watches.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```
