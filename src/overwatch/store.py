from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from overwatch.github import CiStatus, PullRequestRef


def default_db_path() -> Path:
    return Path.home() / ".overwatch" / "overwatch.sqlite3"


@dataclass(frozen=True, slots=True)
class WatchedPullRequest:
    id: int
    url: str
    owner: str
    repo: str
    number: int
    status: str
    provider: str
    model: str | None
    harness: str | None
    autofix: bool
    merge_on_bot_approval: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class WatchedPullRequestSummary:
    id: int
    url: str
    owner: str
    repo: str
    number: int
    status: str
    provider: str
    model: str | None
    harness: str | None
    autofix: bool
    merge_on_bot_approval: bool
    latest_ci_state: str | None
    latest_head_sha: str | None
    latest_summary: str | None
    latest_checked_at: str | None


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists watched_prs (
                    id integer primary key autoincrement,
                    url text not null unique,
                    owner text not null,
                    repo text not null,
                    number integer not null,
                    status text not null default 'unresolved',
                    provider text not null default 'opencode',
                    model text,
                    harness text,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists ci_check_history (
                    id integer primary key autoincrement,
                    watched_pr_id integer not null references watched_prs(id),
                    head_sha text not null,
                    state text not null,
                    summary text not null,
                    details_json text not null,
                    created_at text not null
                );

                create table if not exists resolution_attempts (
                    id integer primary key autoincrement,
                    watched_pr_id integer not null references watched_prs(id),
                    provider text not null,
                    model text,
                    harness text,
                    head_sha text not null,
                    status text not null,
                    error text,
                    created_at text not null,
                    completed_at text
                );
                """
            )
            _ensure_column(
                conn,
                "watched_prs",
                "merge_on_bot_approval",
                "integer not null default 0",
            )
            _ensure_column(conn, "watched_prs", "autofix", "integer not null default 0")

    def watch_pr(
        self,
        pr: PullRequestRef,
        *,
        provider: str,
        model: str | None,
        harness: str | None,
        autofix: bool = False,
        merge_on_bot_approval: bool = False,
    ) -> WatchedPullRequest:
        self.init()
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """
                insert into watched_prs
                    (url, owner, repo, number, provider, model, harness,
                     autofix, merge_on_bot_approval, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(url) do update set
                    status = 'unresolved',
                    provider = excluded.provider,
                    model = excluded.model,
                    harness = excluded.harness,
                    autofix = excluded.autofix,
                    merge_on_bot_approval = excluded.merge_on_bot_approval,
                    updated_at = excluded.updated_at
                """,
                (
                    pr.url,
                    pr.owner,
                    pr.repo,
                    pr.number,
                    provider,
                    model,
                    harness,
                    int(autofix),
                    int(merge_on_bot_approval),
                    now,
                    now,
                ),
            )
            row = conn.execute("select * from watched_prs where url = ?", (pr.url,)).fetchone()
        return _watched_pr(row)

    def unresolved_prs(self) -> list[WatchedPullRequest]:
        self.init()
        with self.connect() as conn:
            rows = conn.execute(
                "select * from watched_prs where status = 'unresolved' order by created_at"
            ).fetchall()
        return [_watched_pr(row) for row in rows]

    def watched_prs(self, *, include_inactive: bool = False) -> list[WatchedPullRequestSummary]:
        self.init()
        inactive_statuses = "'resolved', 'merged', 'closed'"
        where = "" if include_inactive else f"where watched_prs.status not in ({inactive_statuses})"
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select
                    watched_prs.id,
                    watched_prs.url,
                    watched_prs.owner,
                    watched_prs.repo,
                    watched_prs.number,
                    watched_prs.status,
                    watched_prs.provider,
                    watched_prs.model,
                    watched_prs.harness,
                    watched_prs.autofix,
                    watched_prs.merge_on_bot_approval,
                    latest.state as latest_ci_state,
                    latest.head_sha as latest_head_sha,
                    latest.summary as latest_summary,
                    latest.created_at as latest_checked_at
                from watched_prs
                left join ci_check_history latest
                    on latest.id = (
                        select max(id)
                        from ci_check_history
                        where watched_pr_id = watched_prs.id
                    )
                {where}
                order by watched_prs.created_at
                """
            ).fetchall()
        return [_watched_pr_summary(row) for row in rows]

    def record_ci_status(self, pr_id: int, status: CiStatus) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into ci_check_history
                    (watched_pr_id, head_sha, state, summary, details_json, created_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    pr_id,
                    status.head_sha,
                    status.state,
                    status.summary,
                    json.dumps(status.details),
                    _now(),
                ),
            )

    def attempt_count(self, pr_id: int, head_sha: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                select count(*) as count
                from resolution_attempts
                where watched_pr_id = ? and head_sha = ?
                """,
                (pr_id, head_sha),
            ).fetchone()
        return int(row["count"])

    def start_attempt(self, pr: WatchedPullRequest, head_sha: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into resolution_attempts
                    (watched_pr_id, provider, model, harness, head_sha, status, created_at)
                values (?, ?, ?, ?, ?, 'running', ?)
                """,
                (pr.id, pr.provider, pr.model, pr.harness, head_sha, _now()),
            )
            return int(cursor.lastrowid)

    def finish_attempt(self, attempt_id: int, *, status: str, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update resolution_attempts
                set status = ?, error = ?, completed_at = ?
                where id = ?
                """,
                (status, error, _now(), attempt_id),
            )

    def mark_resolved(self, pr_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "update watched_prs set status = 'resolved', updated_at = ? where id = ?",
                (_now(), pr_id),
            )

    def mark_inactive(self, pr_id: int, *, status: str) -> None:
        if status not in {"merged", "closed"}:
            raise ValueError("inactive status must be 'merged' or 'closed'")
        with self.connect() as conn:
            conn.execute(
                "update watched_prs set status = ?, updated_at = ? where id = ?",
                (status, _now(), pr_id),
            )


def _watched_pr(row: sqlite3.Row) -> WatchedPullRequest:
    values = {key: row[key] for key in row.keys()}
    values["autofix"] = bool(values["autofix"])
    values["merge_on_bot_approval"] = bool(values["merge_on_bot_approval"])
    return WatchedPullRequest(**values)


def _watched_pr_summary(row: sqlite3.Row) -> WatchedPullRequestSummary:
    values = {key: row[key] for key in row.keys()}
    values["autofix"] = bool(values["autofix"])
    values["merge_on_bot_approval"] = bool(values["merge_on_bot_approval"])
    return WatchedPullRequestSummary(**values)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def rows_as_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]
