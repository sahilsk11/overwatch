from __future__ import annotations

import asyncio

from overwatch.github import GitHubClient, PullRequestRef
from overwatch.providers import AgentConfig, ProviderRegistry, default_registry
from overwatch.store import Store, WatchedPullRequest


async def run_once(
    store: Store,
    *,
    github: GitHubClient | None = None,
    registry: ProviderRegistry | None = None,
    max_attempts_per_sha: int = 3,
) -> None:
    github = github or GitHubClient()
    registry = registry or default_registry()

    for watched in store.unresolved_prs():
        pr = PullRequestRef(
            owner=watched.owner,
            repo=watched.repo,
            number=watched.number,
            url=watched.url,
        )
        status = github.get_ci_status(pr)
        store.record_ci_status(watched.id, status)

        if status.state == "success":
            store.mark_resolved(watched.id)
            continue
        if status.state != "failure":
            continue
        if store.attempt_count(watched.id, status.head_sha) >= max_attempts_per_sha:
            continue

        await _attempt_fix(store, registry, watched, status.head_sha, status.summary)


async def _attempt_fix(
    store: Store,
    registry: ProviderRegistry,
    watched: WatchedPullRequest,
    head_sha: str,
    summary: str,
) -> None:
    attempt_id = store.start_attempt(watched, head_sha)
    provider = registry.get(watched.provider)
    prompt = _build_prompt(watched, head_sha, summary)
    config = AgentConfig(provider=watched.provider, model=watched.model, harness=watched.harness)
    try:
        await provider.run(prompt, config)
    except Exception as exc:
        store.finish_attempt(attempt_id, status="failed", error=str(exc))
        return
    store.finish_attempt(attempt_id, status="completed")


def run_forever(store: Store, *, interval_seconds: int = 300) -> None:
    async def loop() -> None:
        while True:
            await run_once(store)
            await asyncio.sleep(interval_seconds)

    asyncio.run(loop())


def _build_prompt(watched: WatchedPullRequest, head_sha: str, summary: str) -> str:
    return f"""A GitHub PR has failing CI.

PR: {watched.url}
Repository: {watched.owner}/{watched.repo}
Head SHA: {head_sha}

CI summary:
{summary}

Diagnose the failure, make the smallest correct code change, and run the relevant tests.
Do not amend commits or force push.
"""
