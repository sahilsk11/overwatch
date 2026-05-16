from __future__ import annotations

import asyncio

from overwatch.github import CiStatus, GitHubClient, PullRequestRef, ReviewThread
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

        if status.merged:
            store.mark_inactive(watched.id, status="merged")
            continue
        if status.pr_state == "closed":
            store.mark_inactive(watched.id, status="closed")
            continue
        review_threads = _review_threads_for_work(github, pr, watched)
        if watched.merge_on_bot_approval and _ci_allows_bot_merge(status):
            if review_threads is None:
                continue
            if review_threads:
                attempt_count = store.attempt_count(watched.id, status.head_sha)
                if watched.autofix and attempt_count < max_attempts_per_sha:
                    completed = await _attempt_fix(
                        store,
                        registry,
                        watched,
                        status.head_sha,
                        _review_summary(review_threads),
                    )
                    if completed:
                        github.request_codex_review(pr, status.head_sha)
                continue
            decision = github.get_codex_review_decision(pr, status.head_sha)
            if decision.approved:
                github.merge_pr(pr, status.head_sha)
                store.mark_inactive(watched.id, status="merged")
            continue
        if not watched.autofix:
            continue
        if status.state != "failure" and not review_threads:
            continue
        if store.attempt_count(watched.id, status.head_sha) >= max_attempts_per_sha:
            continue

        summary = _work_summary(status.summary, status.state, review_threads or [])
        completed = await _attempt_fix(store, registry, watched, status.head_sha, summary)
        if completed and watched.merge_on_bot_approval:
            github.request_codex_review(pr, status.head_sha)


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
        return False
    store.finish_attempt(attempt_id, status="completed")
    return True


def run_forever(store: Store, *, interval_seconds: int = 300) -> None:
    async def loop() -> None:
        while True:
            await run_once(store)
            await asyncio.sleep(interval_seconds)

    asyncio.run(loop())


def _review_threads_for_work(
    github: GitHubClient,
    pr: PullRequestRef,
    watched: WatchedPullRequest,
) -> list[ReviewThread] | None:
    if not watched.autofix and not watched.merge_on_bot_approval:
        return []
    try:
        return github.get_unresolved_review_threads(pr)
    except RuntimeError:
        if watched.merge_on_bot_approval:
            return None
        return []


def _review_summary(review_threads: list[ReviewThread]) -> str:
    lines = ["Unresolved review comments:"]
    for thread in review_threads:
        location = ""
        if thread.path and thread.line:
            location = f" on {thread.path}:{thread.line}"
        thread_id = f" thread {thread.id}" if thread.id else ""
        lines.append(
            f"- {thread.author}{location}{thread_id}: {thread.body}\n  {thread.url}".strip()
        )
    return "\n".join(lines)


def _work_summary(ci_summary: str, ci_state: str, review_threads: list[ReviewThread]) -> str:
    parts: list[str] = []
    if ci_state == "failure":
        parts.append(f"CI failure:\n{ci_summary}")
    if review_threads:
        parts.append(_review_summary(review_threads))
    return "\n\n".join(parts)


def _ci_allows_bot_merge(status: CiStatus) -> bool:
    if status.state == "success":
        return True
    return status.state == "unknown" and status.summary == "No CI checks reported."


def _build_prompt(watched: WatchedPullRequest, head_sha: str, summary: str) -> str:
    return f"""A GitHub PR needs automated follow-up.

PR: {watched.url}
Repository: {watched.owner}/{watched.repo}
Head SHA: {head_sha}

Blocking work summary:
{summary}

Address the blocking CI failure or unresolved review comments.
Make the smallest correct code change and run the relevant tests.
For each unresolved review comment, reply on the thread with either what you fixed
or why no code change is needed.
Resolve the review thread after you have addressed it or clearly explained why it is not applicable.
If you are in the target repository on the PR branch, commit the fix and push it.
Do not amend commits or force push.
"""
