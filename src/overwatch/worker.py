from __future__ import annotations

import asyncio
from dataclasses import dataclass

from overwatch.agent_context import AgentContext, AgentPriorTurn
from overwatch.domain import CiSnapshot, PullRequestRef, PullRequestSnapshot, ReviewThread
from overwatch.github import GitHubClient, ci_status_from_snapshots
from overwatch.policy import CiPolicy, CodexApprovalPolicy, MergePolicy, WatchPolicy
from overwatch.providers import (
    AgentConfig,
    ProviderRegistry,
    ProviderRunError,
    ProviderRunResult,
    default_registry,
)
from overwatch.store import Store, WatchedPullRequest


@dataclass(frozen=True, slots=True)
class Observation:
    pr: PullRequestSnapshot
    ci: CiSnapshot


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
        observation = _observe_pr(store, github, pr, watched)
        ci_decision = CiPolicy.classify(observation.ci)
        watch_policy = WatchPolicy(
            autofix=watched.autofix,
            merge_on_bot_approval=watched.merge_on_bot_approval,
        )

        if observation.pr.merged:
            store.mark_inactive(watched.id, status="merged")
            continue
        if observation.pr.state == "closed":
            store.mark_inactive(watched.id, status="closed")
            continue
        review_threads = _review_threads_for_work(github, pr, watched)
        if watched.merge_on_bot_approval and ci_decision.allows_bot_merge:
            if review_threads is None:
                continue
            if review_threads:
                attempt_count = store.attempt_count(watched.id, observation.pr.head_sha)
                if watched.autofix:
                    if _turn_budget_exhausted(store, watched):
                        continue
                    if attempt_count >= max_attempts_per_sha:
                        continue
                    completed = await _attempt_fix(
                        store,
                        registry,
                        watched,
                        observation.pr.head_sha,
                        _review_summary(review_threads),
                    )
                    if completed:
                        refreshed = _refresh_after_provider(store, github, pr, watched)
                        if refreshed is not None:
                            github.request_codex_review(pr, refreshed.pr.head_sha)
                continue
            review_snapshot = github.get_review_snapshot(pr, observation.pr.head_sha)
            approval_decision = CodexApprovalPolicy().evaluate(
                review_snapshot,
                observation.pr.head_sha,
            )
            merge_decision = MergePolicy().evaluate(
                ci=ci_decision,
                approval=approval_decision,
                watch_policy=watch_policy,
                unresolved_review_threads=review_threads,
            )
            if merge_decision.can_merge:
                github.merge_pr(pr, observation.pr.head_sha)
                store.mark_inactive(watched.id, status="merged")
            continue
        if not watched.autofix:
            continue
        if not ci_decision.should_autofix and not review_threads:
            continue
        if _turn_budget_exhausted(store, watched):
            continue
        if store.attempt_count(watched.id, observation.pr.head_sha) >= max_attempts_per_sha:
            continue

        summary = _work_summary(observation.ci, review_threads or [])
        completed = await _attempt_fix(store, registry, watched, observation.pr.head_sha, summary)
        if completed and watched.merge_on_bot_approval:
            refreshed = _refresh_after_provider(store, github, pr, watched)
            if refreshed is not None:
                github.request_codex_review(pr, refreshed.pr.head_sha)


async def _attempt_fix(
    store: Store,
    registry: ProviderRegistry,
    watched: WatchedPullRequest,
    head_sha: str,
    summary: str,
) -> bool:
    prior_turns = _agent_prior_turns(store, watched.id)
    attempt_id = store.start_attempt(watched, head_sha)
    current_watched = store.get_pr(watched.id) or watched
    provider = registry.get(watched.provider)
    prompt = _build_prompt(current_watched, head_sha, summary, prior_turns)
    config = AgentConfig(provider=watched.provider, model=watched.model, harness=watched.harness)
    store.record_attempt_diagnostics(
        attempt_id,
        provider_command=_provider_command(provider, config),
    )
    attempt_finished = False
    try:
        result = await provider.run(prompt, config)
    except ProviderRunError as exc:
        store.finish_attempt(
            attempt_id,
            status="failed",
            error=str(exc),
            provider_command=exc.result.command,
            provider_output=exc.result.output,
        )
        attempt_finished = True
        return False
    except Exception as exc:
        store.finish_attempt(attempt_id, status="failed", error=str(exc))
        attempt_finished = True
        return False
    except BaseException as exc:
        store.finish_attempt(attempt_id, status="failed", error=f"interrupted: {exc}")
        attempt_finished = True
        raise
    else:
        provider_result = result if isinstance(result, ProviderRunResult) else None
        store.finish_attempt(
            attempt_id,
            status="completed",
            provider_command=provider_result.command if provider_result else None,
            provider_output=provider_result.output if provider_result else None,
        )
        attempt_finished = True
        return True
    finally:
        if not attempt_finished:
            store.finish_attempt(
                attempt_id,
                status="failed",
                error="attempt interrupted before provider completion",
            )


def _refresh_after_provider(
    store: Store,
    github: GitHubClient,
    pr: PullRequestRef,
    watched: WatchedPullRequest,
) -> Observation | None:
    try:
        observation = _observe_pr(store, github, pr, watched)
    except RuntimeError:
        return None
    if observation.pr.merged:
        store.mark_inactive(watched.id, status="merged")
        return None
    if observation.pr.state == "closed":
        store.mark_inactive(watched.id, status="closed")
        return None
    return observation


def _turn_budget_exhausted(store: Store, watched: WatchedPullRequest) -> bool:
    if watched.turns_used < watched.max_turns:
        return False
    store.mark_needs_human(watched.id)
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


def _work_summary(ci_snapshot: CiSnapshot, review_threads: list[ReviewThread]) -> str:
    parts: list[str] = []
    if ci_snapshot.rollup_state == "failure":
        parts.append(f"CI failure:\n{_ci_failure_summary(ci_snapshot)}")
    if review_threads:
        parts.append(_review_summary(review_threads))
    return "\n\n".join(parts)


def _observe_pr(
    store: Store,
    github: GitHubClient,
    pr: PullRequestRef,
    watched: WatchedPullRequest,
) -> Observation:
    pr_snapshot = github.get_pr_snapshot(pr)
    ci_snapshot = github.get_ci_snapshot(pr, pr_snapshot)
    store.record_ci_status(watched.id, ci_status_from_snapshots(pr_snapshot, ci_snapshot))
    return Observation(pr=pr_snapshot, ci=ci_snapshot)


def _ci_failure_summary(snapshot: CiSnapshot) -> str:
    contexts = [
        context
        for group in (
            snapshot.rollup_contexts,
            snapshot.legacy_statuses,
            snapshot.check_runs,
            snapshot.workflow_runs,
        )
        for context in (group or ())
        if context.is_failing
    ]
    if not contexts and snapshot.source_errors:
        return "\n".join(f"{source}: {error}" for source, error in snapshot.source_errors.items())
    lines = []
    for context in contexts:
        state = context.state or context.status or "unknown"
        conclusion = context.conclusion or ""
        url = f" {context.url}" if context.url else ""
        lines.append(f"{context.source} {context.name}: {state} {conclusion}{url}".strip())
    return "\n".join(lines) if lines else "CI failed, but no failing context details were reported."


def _agent_prior_turns(store: Store, pr_id: int) -> tuple[AgentPriorTurn, ...]:
    turns = reversed(store.watch_turns(pr_id))
    return tuple(
        AgentPriorTurn(
            turn_number=turn.turn_number,
            starting_head_sha=turn.starting_head_sha,
            status=turn.status,
            completed_at=turn.completed_at,
        )
        for turn in turns
        if turn.status != "running"
    )


def _build_prompt(
    watched: WatchedPullRequest,
    head_sha: str,
    summary: str,
    prior_turns: tuple[AgentPriorTurn, ...] = (),
) -> str:
    include_durable_context = watched.session_strategy in {"context-summary", "attached-session"}
    return AgentContext(
        pr_url=watched.url,
        repository=f"{watched.owner}/{watched.repo}",
        pr_number=watched.number,
        head_sha=head_sha,
        provider=watched.provider,
        model=watched.model,
        harness=watched.harness,
        autofix=watched.autofix,
        merge_on_bot_approval=watched.merge_on_bot_approval,
        max_turns=watched.max_turns,
        turns_used=watched.turns_used,
        context_summary=watched.context_summary if include_durable_context else "",
        current_blockers=summary,
        prior_turns=prior_turns if include_durable_context else (),
    ).render_prompt()


def _provider_command(provider: object, config: AgentConfig) -> str | None:
    command_display = getattr(provider, "command_display", None)
    if callable(command_display):
        return str(command_display(config))
    return None
