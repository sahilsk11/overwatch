from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from overwatch.github import parse_pr_url
from overwatch.store import Store, default_db_path
from overwatch.worker import run_forever, run_once


def main() -> None:
    parser = argparse.ArgumentParser(prog="overwatch")
    parser.add_argument("pr_link", nargs="?", help="GitHub PR URL to watch, or 'list'")
    parser.add_argument("--db", type=Path, default=default_db_path(), help="SQLite database path")
    parser.add_argument("--all", action="store_true", help="Include merged and closed PRs in list")
    parser.add_argument(
        "--provider",
        default="opencode",
        choices=["opencode", "codex", "claude-code"],
    )
    parser.add_argument("--model", help="Model to pass to the coding agent")
    parser.add_argument("--harness", help="Harness name to pass to the coding agent")
    parser.add_argument(
        "--merge-on-bot-approval",
        action="store_true",
        help="Merge after CI passes if the latest supported bot review approves the PR",
    )
    parser.add_argument("--run-once", action="store_true", help="Check unresolved PRs once")
    parser.add_argument("--worker", action="store_true", help="Run the polling worker")
    parser.add_argument("--interval", type=int, default=300, help="Worker interval in seconds")
    args = parser.parse_args()

    store = Store(args.db)
    store.init()

    if args.run_once:
        asyncio.run(run_once(store))
        return
    if args.worker:
        run_forever(store, interval_seconds=args.interval)
        return
    if args.pr_link == "list":
        _print_watched_prs(store, include_inactive=args.all)
        return
    if not args.pr_link:
        parser.error("provide a GitHub PR URL, 'list', --run-once, or --worker")

    pr = parse_pr_url(args.pr_link)
    watched = store.watch_pr(
        pr,
        provider=args.provider,
        model=args.model,
        harness=args.harness,
        merge_on_bot_approval=args.merge_on_bot_approval,
    )
    print(f"watching {watched.url} with {watched.provider}")


def _print_watched_prs(store: Store, *, include_inactive: bool) -> None:
    rows = store.watched_prs(include_inactive=include_inactive)
    if not rows:
        print("No watched PRs.")
        return

    print("STATUS   CI       PR  PROVIDER  MODEL  URL")
    for row in rows:
        ci_state = row.latest_ci_state or "unknown"
        model = row.model or "-"
        print(
            f"{row.status:<8} {ci_state:<8} "
            f"#{row.number:<3} {row.provider:<9} {model:<6} {row.url}"
        )


if __name__ == "__main__":
    main()
