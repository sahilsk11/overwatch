from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from overwatch.github import parse_pr_url
from overwatch.store import Store, default_db_path
from overwatch.worker import run_forever, run_once


def main() -> None:
    parser = argparse.ArgumentParser(prog="overwatch")
    parser.add_argument("pr_link", nargs="?", help="GitHub PR URL to watch")
    parser.add_argument("--db", type=Path, default=default_db_path(), help="SQLite database path")
    parser.add_argument(
        "--provider",
        default="opencode",
        choices=["opencode", "codex", "claude-code"],
    )
    parser.add_argument("--model", help="Model to pass to the coding agent")
    parser.add_argument("--harness", help="Harness name to pass to the coding agent")
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
    if not args.pr_link:
        parser.error("provide a GitHub PR URL, --run-once, or --worker")

    pr = parse_pr_url(args.pr_link)
    watched = store.watch_pr(pr, provider=args.provider, model=args.model, harness=args.harness)
    print(f"watching {watched.url} with {watched.provider}")


if __name__ == "__main__":
    main()
