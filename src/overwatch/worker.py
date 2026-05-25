from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass

from overwatch.github import GitHubClient, PullRequestRef
from overwatch.providers import AgentConfig, ProviderRegistry, default_registry
from overwatch.store import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    Store,
    WatchedPullRequest,
    WatchTurnEvent,
)


@dataclass(frozen=True, slots=True)
class ClaimedWatch:
    watch: WatchedPullRequest
    turn: WatchTurnEvent


async def run_tick(
    store: Store,
    *,
    github: object | None = None,
    registry: ProviderRegistry | None = None,
    provider_id: str | None = None,
    model: str | None = DEFAULT_MODEL,
    max_attempts_per_sha: int | None = None,
) -> None:
    _ = max_attempts_per_sha
    github = github or GitHubClient()
    prune_inactive_watches(store, github=github)
    watches = _claim_runnable_watches(store)
    if not watches:
        return
    registry = registry or default_registry()
    provider_id = provider_id or DEFAULT_PROVIDER
    model = model if model is not None else DEFAULT_MODEL
    try:
        provider = registry.get(provider_id)
        await provider.run(
            build_supervisor_prompt(store, [claimed.watch for claimed in watches]),
            AgentConfig(provider=provider_id, model=model),
        )
    except Exception:
        _finish_claimed_turns(store, watches, status="failed")
        return
    _finish_claimed_turns(store, watches, status="completed")


def run_forever(store: Store, *, interval_seconds: int = 300) -> None:
    async def loop() -> None:
        while True:
            await run_tick(store)
            await asyncio.sleep(interval_seconds)

    asyncio.run(loop())


def build_supervisor_prompt(store: Store, watches: list[WatchedPullRequest]) -> str:
    db_arg = shlex.quote(str(store.path))
    rows = "\n".join(_watch_line(watch) for watch in watches)
    child_prompt = _child_prompt_template()
    return f"""You are Overwatch.

This program is intentionally just a wake-up call. The durable state is a small
SQLite watch list at:

{store.path}

Start by running:

```sh
overwatch --db {db_arg} list
```

Active watches:
{rows}

For each PR that still needs attention:

1. Use the GitHub CLI (`gh`) to inspect the PR, comments, review threads, CI,
   and merge state.
2. Respect the watch flags in the active watch line. If autofix is disabled,
   do not spawn a repair agent for that PR. If merge-on-bot-approval is
   disabled, do not merge it.
3. If merge-on-bot-approval is enabled and the current head does not already
   have a Codex review or pending Codex review request, comment `@codex review`
   on the PR with `gh`. Do not post duplicate review requests for the same head.
4. If autofix is enabled and the PR needs work, spawn a separate child Codex
   process for that PR. Do not mix multiple PR repairs in this supervisor run.
5. Give the child agent this prompt, filled in with the PR URL, watch policy,
   watch context, and session id:

```text
{child_prompt}
```

If the PR only needs review, merge, a comment, or human follow-up, use `gh` or
`overwatch --pause/--stop` directly. Keep your final supervisor output short:
one line per PR with what happened.
"""


def _watch_line(watch: WatchedPullRequest) -> str:
    context = " ".join(watch.context_summary.split()) or "-"
    session = watch.session_id or "-"
    return (
        f"- id={watch.id} url={watch.url} provider={watch.provider} "
        f"model={watch.model or '-'} turns={watch.turns_used}/{watch.max_turns} "
        f"autofix={_yes_no(watch.autofix)} "
        f"merge_on_bot_approval={_yes_no(watch.merge_on_bot_approval)} "
        f"session={session} context={context}"
    )


def _child_prompt_template() -> str:
    return """You are a child Overwatch repair agent for exactly one GitHub PR.

PR: <PR URL>
Watch context: <WATCH CONTEXT>
Watch policy: autofix=<yes/no>; merge_on_bot_approval=<yes/no>; turns=<used/max>
Original session, if any: <SESSION ID>

Use `gh` to inspect the PR, comments, review threads, and CI. Fix the real
blocker, run relevant tests, commit, and push. If no code change is appropriate,
explain why on the PR. Do not work on unrelated PRs.

Make one focused repair attempt, then stop. Do not poll forever. The next
Overwatch tick will re-inspect the PR and decide what to do next.

End with what changed, tests run, whether you pushed, and anything still blocked.
"""


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def prune_inactive_watches(store: Store, *, github: object) -> None:
    for watch in store.unresolved_prs():
        pr = PullRequestRef(
            owner=watch.owner,
            repo=watch.repo,
            number=watch.number,
            url=watch.url,
        )
        try:
            snapshot = github.get_pr_snapshot(pr)
        except RuntimeError:
            continue
        if snapshot.merged:
            store.mark_inactive(watch.id, status="merged")
        elif snapshot.state.lower() == "closed":
            store.mark_inactive(watch.id, status="closed")


def _claim_runnable_watches(store: Store) -> list[ClaimedWatch]:
    claimed: list[ClaimedWatch] = []
    for watch in store.unresolved_prs():
        if watch.turns_used >= watch.max_turns:
            store.mark_needs_human(watch.id)
            continue
        try:
            turn = store.start_supervisor_turn(watch)
        except RuntimeError:
            continue
        refreshed = store.get_pr(watch.id)
        claimed.append(ClaimedWatch(watch=refreshed or watch, turn=turn))
    return claimed


def _finish_claimed_turns(store: Store, watches: list[ClaimedWatch], *, status: str) -> None:
    for claimed in watches:
        store.finish_supervisor_turn(claimed.turn.id, status=status)
