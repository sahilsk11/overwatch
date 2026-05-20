from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import NoReturn

from overwatch.github import GitHubClient, PullRequestRef, parse_pr_url
from overwatch.store import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    DEFAULT_SESSION_STRATEGY,
    DEFAULT_WORKER_INTERVAL_SECONDS,
    SESSION_STRATEGIES,
    Store,
    default_db_path,
)
from overwatch.worker import run_forever, run_once


def main() -> None:
    parser = argparse.ArgumentParser(prog="overwatch")
    parser.add_argument("pr_link", nargs="?", help="GitHub PR URL to watch, or 'list'")
    parser.add_argument("--db", type=Path, default=default_db_path(), help="SQLite database path")
    parser.add_argument("--all", action="store_true", help="Include merged and closed PRs in list")
    parser.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        choices=["opencode", "codex", "claude-code"],
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Model to pass to the coding agent",
    )
    parser.add_argument("--harness", help="Harness name to pass to the coding agent")
    parser.add_argument(
        "--context",
        default="",
        help="Durable context summary to include in future agent prompts",
    )
    parser.add_argument(
        "--context-file",
        type=Path,
        help="Read durable context summary from a file and include it in future agent prompts",
    )
    parser.add_argument(
        "--session-strategy",
        choices=sorted(SESSION_STRATEGIES),
        default=DEFAULT_SESSION_STRATEGY,
        help=(
            "Agent session behavior: fresh omits durable context, context-summary prepends "
            "Overwatch context, attached-session records an owned session ID"
        ),
    )
    parser.add_argument(
        "--session-id",
        help="Existing session ID for --session-strategy attached-session",
    )
    parser.add_argument(
        "--turns",
        type=_turn_budget,
        default=3,
        help="Maximum provider turns for this watch, from 1 to 10",
    )
    parser.add_argument(
        "--autofix",
        action="store_true",
        help="Run the configured coding agent for failing CI or unresolved review comments",
    )
    parser.add_argument(
        "--merge-on-bot-approval",
        action="store_true",
        help="Merge after CI passes if the latest supported bot review approves the PR",
    )
    parser.add_argument("--run-once", action="store_true", help="Check unresolved PRs once")
    parser.add_argument("--worker", action="store_true", help="Run the polling worker")
    parser.add_argument("--serve", action="store_true", help="Run the FastAPI backend")
    parser.add_argument("--pause", metavar="WATCH", help="Pause a watch by database ID or PR URL")
    parser.add_argument(
        "--resume",
        metavar="WATCH",
        help="Resume a paused watch by database ID or PR URL",
    )
    parser.add_argument("--stop", metavar="WATCH", help="Stop a watch by database ID or PR URL")
    parser.add_argument("--host", default="127.0.0.1", help="Backend host for --serve")
    parser.add_argument("--port", type=int, default=8000, help="Backend port for --serve")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_WORKER_INTERVAL_SECONDS,
        help="Worker interval in seconds",
    )
    args = parser.parse_args()

    store = Store(args.db)
    store.init()

    control_actions = [value for value in (args.pause, args.resume, args.stop) if value]
    if len(control_actions) > 1:
        parser.error("choose only one of --pause, --resume, or --stop")
    if args.pause:
        watched = store.pause_watch(_watch_id(store, args.pause))
        print(f"paused {watched.url}")
        return
    if args.resume:
        watched = store.resume_watch(_watch_id(store, args.resume))
        print(f"resumed {watched.url}")
        return
    if args.stop:
        watched = store.stop_watch(_watch_id(store, args.stop))
        print(f"stopped {watched.url}")
        return

    if args.run_once:
        asyncio.run(run_once(store))
        return
    if args.worker:
        run_forever(store, interval_seconds=args.interval)
        return
    if args.serve:
        _serve_api(store, host=args.host, port=args.port)
        return
    if args.pr_link == "list":
        _print_watched_prs(store, include_inactive=args.all)
        return
    if not args.pr_link:
        parser.error("provide a GitHub PR URL, 'list', --run-once, or --worker")

    pr = parse_pr_url(args.pr_link)
    context_summary = _context_summary(args.context, args.context_file)
    try:
        _validate_session_options(args.session_strategy, args.session_id)
    except ValueError as exc:
        parser.error(str(exc))
    warning = _session_warning(args.session_strategy)
    if warning:
        print(warning, file=sys.stderr)
    watched = store.watch_pr(
        pr,
        provider=args.provider,
        model=args.model,
        harness=args.harness,
        context_summary=context_summary,
        session_strategy=args.session_strategy,
        session_id=args.session_id,
        autofix=args.autofix,
        merge_on_bot_approval=args.merge_on_bot_approval,
        max_turns=args.turns,
    )
    print(
        f"watching {watched.url} with {watched.provider} "
        f"({watched.turns_used}/{watched.max_turns} turns, "
        f"session={watched.session_strategy})"
    )


def _print_watched_prs(store: Store, *, include_inactive: bool) -> None:
    rows = store.watched_prs(include_inactive=include_inactive)
    if not rows:
        print("No watched PRs.")
        return

    github = GitHubClient()
    print(
        "STATUS       WORKER    CI       PR  PROVIDER  MODEL  TURNS  SESSION          "
        "COMMENTS  ACTIVE  LAST ATTEMPT  LAST ERROR  URL"
    )
    for row in rows:
        ci_state = row.latest_ci_state or "unknown"
        model = row.model or "-"
        turns = f"{row.turns_used}/{row.max_turns}"
        active = (
            f"{row.active_attempt_elapsed_seconds}s"
            if row.active_attempt_elapsed_seconds is not None
            else "-"
        )
        last_attempt = row.last_attempt_status or "-"
        last_error = _clip(row.last_error or "-", 24)
        pr = PullRequestRef(owner=row.owner, repo=row.repo, number=row.number, url=row.url)
        comment_count = _unresolved_comment_count(github, pr)
        print(
            f"{row.status:<12} {row.worker_status:<9} {ci_state:<8} "
            f"#{row.number:<3} {row.provider:<9} {model:<6} {turns:<6} "
            f"{row.session_strategy:<16} {comment_count:<8} "
            f"{active:<7} {last_attempt:<13} {last_error:<24} {row.url}"
        )


def _unresolved_comment_count(github: GitHubClient, pr: PullRequestRef) -> str:
    try:
        threads = github.get_unresolved_review_threads(pr)
    except RuntimeError as exc:
        return f"? ({exc})"
    return str(len(threads))


def _serve_api(store: Store, *, host: str, port: int) -> NoReturn:
    import uvicorn

    from overwatch.api import create_app

    uvicorn.run(create_app(store=store), host=host, port=port)
    raise SystemExit(0)


def _context_summary(context: str, context_file: Path | None) -> str:
    parts = [context.strip()] if context.strip() else []
    if context_file is not None:
        parts.append(context_file.expanduser().read_text(encoding="utf-8").strip())
    return "\n\n".join(part for part in parts if part)


def _turn_budget(value: str) -> int:
    try:
        turns = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--turns must be an integer") from exc
    if turns < 1 or turns > 10:
        raise argparse.ArgumentTypeError("--turns must be between 1 and 10")
    return turns


def _watch_id(store: Store, value: str) -> int:
    if value.isdigit():
        return int(value)
    for row in store.watched_prs(include_inactive=True):
        if row.url == value:
            return row.id
    raise SystemExit(f"watch not found: {value}")


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _validate_session_options(session_strategy: str, session_id: str | None) -> None:
    if session_strategy not in SESSION_STRATEGIES:
        expected = ", ".join(sorted(SESSION_STRATEGIES))
        raise ValueError(f"--session-strategy must be one of: {expected}")
    has_session_id = bool(session_id and session_id.strip())
    if session_strategy == "attached-session" and not has_session_id:
        raise ValueError("--session-id is required with --session-strategy attached-session")
    if session_strategy != "attached-session" and has_session_id:
        raise ValueError("--session-id can only be used with --session-strategy attached-session")


def _session_warning(session_strategy: str) -> str | None:
    if session_strategy != "attached-session":
        return None
    return (
        "Warning: attached-session is advanced. Overwatch records the session ID and treats "
        "that session as owned by the watch, but provider session resume is not implemented yet."
    )


if __name__ == "__main__":
    main()
