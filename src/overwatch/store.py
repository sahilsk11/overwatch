from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from overwatch.github import CiStatus, PullRequestRef

SESSION_STRATEGIES = frozenset({"fresh", "context-summary", "attached-session"})
DEFAULT_SESSION_STRATEGY = "context-summary"
DEFAULT_PROVIDER = "codex"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_WORKER_INTERVAL_SECONDS = 60
DONE_STATUSES = frozenset({"resolved", "merged", "closed", "stopped"})
CONTROL_STATUSES = frozenset({"unresolved", "paused", "stopped"})
MAX_STORED_PROVIDER_OUTPUT_CHARS = 12_000
MAX_STORED_PROVIDER_ERROR_CHARS = 4_000


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
    context_summary: str
    session_strategy: str
    session_id: str | None
    autofix: bool
    merge_on_bot_approval: bool
    max_turns: int
    turns_used: int
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
    context_summary: str
    session_strategy: str
    session_id: str | None
    autofix: bool
    merge_on_bot_approval: bool
    max_turns: int
    turns_used: int
    created_at: str
    updated_at: str
    latest_ci_state: str | None
    latest_head_sha: str | None
    latest_summary: str | None
    latest_checked_at: str | None
    worker_status: str
    active_attempt_id: int | None
    active_attempt_started_at: str | None
    active_attempt_elapsed_seconds: int | None
    active_attempt_status: str | None
    last_attempt_status: str | None
    last_attempt_completed_at: str | None
    last_provider_command: str | None
    last_provider_output: str | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class CiHistoryEvent:
    id: int
    head_sha: str
    state: str
    summary: str
    details: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class ResolutionAttemptEvent:
    id: int
    watch_turn_id: int | None
    turn_number: int | None
    provider: str
    model: str | None
    harness: str | None
    head_sha: str
    status: str
    provider_command: str | None
    provider_output: str | None
    error: str | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class WatchTurnEvent:
    id: int
    turn_number: int
    starting_head_sha: str
    status: str
    created_at: str
    completed_at: str | None


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
                    provider text not null default 'codex',
                    model text default 'gpt-5.5',
                    harness text,
                    context_summary text not null default '',
                    session_strategy text not null default 'context-summary',
                    session_id text,
                    max_turns integer not null default 3,
                    turns_used integer not null default 0,
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

                create table if not exists watch_turns (
                    id integer primary key autoincrement,
                    watched_pr_id integer not null references watched_prs(id),
                    turn_number integer not null,
                    starting_head_sha text not null,
                    status text not null,
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
            _ensure_column(conn, "watched_prs", "context_summary", "text not null default ''")
            _ensure_column(
                conn,
                "watched_prs",
                "session_strategy",
                "text not null default 'context-summary'",
            )
            _ensure_column(conn, "watched_prs", "session_id", "text")
            _ensure_column(conn, "watched_prs", "max_turns", "integer not null default 3")
            _ensure_column(conn, "watched_prs", "turns_used", "integer not null default 0")
            _ensure_column(conn, "resolution_attempts", "watch_turn_id", "integer")
            _ensure_column(conn, "resolution_attempts", "provider_command", "text")
            _ensure_column(conn, "resolution_attempts", "provider_output", "text")

    def watch_pr(
        self,
        pr: PullRequestRef,
        *,
        provider: str,
        model: str | None,
        harness: str | None,
        context_summary: str = "",
        session_strategy: str = DEFAULT_SESSION_STRATEGY,
        session_id: str | None = None,
        autofix: bool = False,
        merge_on_bot_approval: bool = False,
        max_turns: int = 3,
    ) -> WatchedPullRequest:
        if max_turns < 1 or max_turns > 10:
            raise ValueError("max_turns must be between 1 and 10")
        _validate_session(session_strategy, session_id)
        self.init()
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """
                insert into watched_prs
                    (url, owner, repo, number, provider, model, harness, context_summary,
                     session_strategy, session_id, autofix, merge_on_bot_approval,
                     max_turns, turns_used,
                     created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                on conflict(url) do update set
                    status = 'unresolved',
                    provider = excluded.provider,
                    model = excluded.model,
                    harness = excluded.harness,
                    context_summary = excluded.context_summary,
                    session_strategy = excluded.session_strategy,
                    session_id = excluded.session_id,
                    autofix = excluded.autofix,
                    merge_on_bot_approval = excluded.merge_on_bot_approval,
                    max_turns = excluded.max_turns,
                    turns_used = 0,
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
                    context_summary,
                    session_strategy,
                    session_id.strip() if session_id else None,
                    int(autofix),
                    int(merge_on_bot_approval),
                    max_turns,
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

    def get_pr(self, pr_id: int) -> WatchedPullRequest | None:
        self.init()
        with self.connect() as conn:
            row = conn.execute("select * from watched_prs where id = ?", (pr_id,)).fetchone()
        return _watched_pr(row) if row else None

    def watched_prs(self, *, include_inactive: bool = False) -> list[WatchedPullRequestSummary]:
        self.init()
        inactive_statuses = ", ".join(f"'{status}'" for status in sorted(DONE_STATUSES))
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
                    watched_prs.context_summary,
                    watched_prs.session_strategy,
                    watched_prs.session_id,
                    watched_prs.autofix,
                    watched_prs.merge_on_bot_approval,
                    watched_prs.max_turns,
                    watched_prs.turns_used,
                    watched_prs.created_at,
                    watched_prs.updated_at,
                    latest.state as latest_ci_state,
                    latest.head_sha as latest_head_sha,
                    latest.summary as latest_summary,
                    latest.created_at as latest_checked_at,
                    running.id as active_attempt_id,
                    running.created_at as active_attempt_started_at,
                    running.status as active_attempt_status,
                    last_attempt.status as last_attempt_status,
                    last_attempt.completed_at as last_attempt_completed_at,
                    last_attempt.provider_command as last_provider_command,
                    last_attempt.provider_output as last_provider_output,
                    last_attempt.error as last_error
                from watched_prs
                left join ci_check_history latest
                    on latest.id = (
                        select max(id)
                        from ci_check_history
                        where watched_pr_id = watched_prs.id
                    )
                left join resolution_attempts running
                    on running.id = (
                        select id
                        from resolution_attempts
                        where watched_pr_id = watched_prs.id and status = 'running'
                        order by created_at desc, id desc
                        limit 1
                    )
                left join resolution_attempts last_attempt
                    on last_attempt.id = (
                        select max(id)
                        from resolution_attempts
                        where watched_pr_id = watched_prs.id
                    )
                {where}
                order by watched_prs.created_at desc, watched_prs.id desc
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

    def pr_events(self, pr_id: int) -> tuple[list[CiHistoryEvent], list[ResolutionAttemptEvent]]:
        self.init()
        with self.connect() as conn:
            ci_rows = conn.execute(
                """
                select id, head_sha, state, summary, details_json, created_at
                from ci_check_history
                where watched_pr_id = ?
                order by created_at desc, id desc
                """,
                (pr_id,),
            ).fetchall()
            attempt_rows = conn.execute(
                """
                select
                    resolution_attempts.id,
                    resolution_attempts.watch_turn_id,
                    watch_turns.turn_number,
                    resolution_attempts.provider,
                    resolution_attempts.model,
                    resolution_attempts.harness,
                    resolution_attempts.head_sha,
                    resolution_attempts.status,
                    resolution_attempts.provider_command,
                    resolution_attempts.provider_output,
                    resolution_attempts.error,
                    resolution_attempts.created_at,
                    resolution_attempts.completed_at
                from resolution_attempts
                left join watch_turns on watch_turns.id = resolution_attempts.watch_turn_id
                where resolution_attempts.watched_pr_id = ?
                order by resolution_attempts.created_at desc, resolution_attempts.id desc
                """,
                (pr_id,),
            ).fetchall()
        return (
            [_ci_history_event(row) for row in ci_rows],
            [_resolution_attempt_event(row) for row in attempt_rows],
        )

    def watch_turns(self, pr_id: int) -> list[WatchTurnEvent]:
        self.init()
        with self.connect() as conn:
            rows = conn.execute(
                """
                select id, turn_number, starting_head_sha, status, created_at, completed_at
                from watch_turns
                where watched_pr_id = ?
                order by turn_number desc, id desc
                """,
                (pr_id,),
            ).fetchall()
        return [_watch_turn_event(row) for row in rows]

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
            row = conn.execute(
                "select status, max_turns, turns_used from watched_prs where id = ?",
                (pr.id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"watched PR {pr.id} does not exist")
            if str(row["status"]) != "unresolved":
                raise RuntimeError(f"watch is {row['status']}")
            turns_used = int(row["turns_used"])
            max_turns = int(row["max_turns"])
            if turns_used >= max_turns:
                raise RuntimeError("turn budget exhausted")
            turn_number = turns_used + 1
            now = _now()
            conn.execute(
                """
                update watched_prs
                set turns_used = ?, updated_at = ?
                where id = ?
                """,
                (turn_number, now, pr.id),
            )
            turn_cursor = conn.execute(
                """
                insert into watch_turns
                    (watched_pr_id, turn_number, starting_head_sha, status, created_at)
                values (?, ?, ?, 'running', ?)
                """,
                (pr.id, turn_number, head_sha, now),
            )
            cursor = conn.execute(
                """
                insert into resolution_attempts
                    (watched_pr_id, watch_turn_id, provider, model, harness, head_sha, status,
                     created_at)
                values (?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    pr.id,
                    int(turn_cursor.lastrowid),
                    pr.provider,
                    pr.model,
                    pr.harness,
                    head_sha,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def start_supervisor_turn(self, pr: WatchedPullRequest) -> WatchTurnEvent:
        with self.connect() as conn:
            row = conn.execute(
                "select status, max_turns, turns_used from watched_prs where id = ?",
                (pr.id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"watched PR {pr.id} does not exist")
            if str(row["status"]) != "unresolved":
                raise RuntimeError(f"watch is {row['status']}")
            turns_used = int(row["turns_used"])
            max_turns = int(row["max_turns"])
            if turns_used >= max_turns:
                raise RuntimeError("turn budget exhausted")
            turn_number = turns_used + 1
            now = _now()
            conn.execute(
                """
                update watched_prs
                set turns_used = ?, updated_at = ?
                where id = ?
                """,
                (turn_number, now, pr.id),
            )
            cursor = conn.execute(
                """
                insert into watch_turns
                    (watched_pr_id, turn_number, starting_head_sha, status, created_at)
                values (?, ?, 'supervisor-tick', 'running', ?)
                """,
                (pr.id, turn_number, now),
            )
            row = conn.execute(
                """
                select id, turn_number, starting_head_sha, status, created_at, completed_at
                from watch_turns
                where id = ?
                """,
                (int(cursor.lastrowid),),
            ).fetchone()
        return _watch_turn_event(row)

    def finish_supervisor_turn(self, turn_id: int, *, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update watch_turns
                set status = ?, completed_at = ?
                where id = ?
                """,
                (status, _now(), turn_id),
            )

    def record_attempt_diagnostics(
        self,
        attempt_id: int,
        *,
        provider_command: str | None = None,
        provider_output: str | None = None,
    ) -> None:
        command = _truncate_nullable(provider_command, MAX_STORED_PROVIDER_ERROR_CHARS)
        output = _truncate_nullable(provider_output, MAX_STORED_PROVIDER_OUTPUT_CHARS)
        if command is None and output is None:
            return
        with self.connect() as conn:
            conn.execute(
                """
                update resolution_attempts
                set provider_command = coalesce(?, provider_command),
                    provider_output = coalesce(?, provider_output)
                where id = ?
                """,
                (command, output, attempt_id),
            )

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        error: str | None = None,
        provider_command: str | None = None,
        provider_output: str | None = None,
    ) -> None:
        with self.connect() as conn:
            now = _now()
            conn.execute(
                """
                update resolution_attempts
                set status = ?,
                    error = ?,
                    provider_command = coalesce(?, provider_command),
                    provider_output = coalesce(?, provider_output),
                    completed_at = ?
                where id = ?
                """,
                (
                    status,
                    _truncate_nullable(error, MAX_STORED_PROVIDER_ERROR_CHARS),
                    _truncate_nullable(provider_command, MAX_STORED_PROVIDER_ERROR_CHARS),
                    _truncate_nullable(provider_output, MAX_STORED_PROVIDER_OUTPUT_CHARS),
                    now,
                    attempt_id,
                ),
            )
            conn.execute(
                """
                update watch_turns
                set status = ?, completed_at = ?
                where id = (
                    select watch_turn_id
                    from resolution_attempts
                    where id = ?
                )
                """,
                (status, now, attempt_id),
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

    def mark_needs_human(self, pr_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "update watched_prs set status = 'needs-human', updated_at = ? where id = ?",
                (_now(), pr_id),
            )

    def pause_watch(self, pr_id: int) -> WatchedPullRequest:
        return self._set_control_status(pr_id, "paused")

    def resume_watch(self, pr_id: int) -> WatchedPullRequest:
        self.init()
        with self.connect() as conn:
            row = conn.execute("select status from watched_prs where id = ?", (pr_id,)).fetchone()
            if row is None:
                raise ValueError(f"watched PR {pr_id} does not exist")
            if row["status"] != "paused":
                raise ValueError("only paused watches can be resumed")
            conn.execute(
                "update watched_prs set status = 'unresolved', updated_at = ? where id = ?",
                (_now(), pr_id),
            )
            updated = conn.execute("select * from watched_prs where id = ?", (pr_id,)).fetchone()
        return _watched_pr(updated)

    def stop_watch(self, pr_id: int) -> WatchedPullRequest:
        return self._set_control_status(pr_id, "stopped")

    def _set_control_status(self, pr_id: int, status: str) -> WatchedPullRequest:
        if status not in CONTROL_STATUSES:
            raise ValueError(f"unsupported control status {status}")
        self.init()
        with self.connect() as conn:
            row = conn.execute("select status from watched_prs where id = ?", (pr_id,)).fetchone()
            if row is None:
                raise ValueError(f"watched PR {pr_id} does not exist")
            if status == "paused" and row["status"] == "stopped":
                raise ValueError("stopped watches cannot be paused")
            if row["status"] in {"resolved", "merged", "closed"}:
                raise ValueError(f"cannot update {row['status']} watch")
            conn.execute(
                "update watched_prs set status = ?, updated_at = ? where id = ?",
                (status, _now(), pr_id),
            )
            updated = conn.execute("select * from watched_prs where id = ?", (pr_id,)).fetchone()
        return _watched_pr(updated)


def _watched_pr(row: sqlite3.Row) -> WatchedPullRequest:
    values = {key: row[key] for key in row.keys()}
    values["autofix"] = bool(values["autofix"])
    values["merge_on_bot_approval"] = bool(values["merge_on_bot_approval"])
    return WatchedPullRequest(**values)


def _watched_pr_summary(row: sqlite3.Row) -> WatchedPullRequestSummary:
    values = {key: row[key] for key in row.keys()}
    values["autofix"] = bool(values["autofix"])
    values["merge_on_bot_approval"] = bool(values["merge_on_bot_approval"])
    values["active_attempt_elapsed_seconds"] = _elapsed_seconds(
        values["active_attempt_started_at"]
    )
    values["worker_status"] = _worker_status(
        status=str(values["status"]),
        active_attempt_id=values["active_attempt_id"],
    )
    return WatchedPullRequestSummary(**values)


def _ci_history_event(row: sqlite3.Row) -> CiHistoryEvent:
    return CiHistoryEvent(
        id=int(row["id"]),
        head_sha=str(row["head_sha"]),
        state=str(row["state"]),
        summary=str(row["summary"]),
        details=json.loads(row["details_json"]),
        created_at=str(row["created_at"]),
    )


def _resolution_attempt_event(row: sqlite3.Row) -> ResolutionAttemptEvent:
    return ResolutionAttemptEvent(
        id=int(row["id"]),
        watch_turn_id=row["watch_turn_id"],
        turn_number=row["turn_number"],
        provider=str(row["provider"]),
        model=row["model"],
        harness=row["harness"],
        head_sha=str(row["head_sha"]),
        status=str(row["status"]),
        provider_command=row["provider_command"],
        provider_output=row["provider_output"],
        error=row["error"],
        created_at=str(row["created_at"]),
        completed_at=row["completed_at"],
    )


def _watch_turn_event(row: sqlite3.Row) -> WatchTurnEvent:
    return WatchTurnEvent(
        id=int(row["id"]),
        turn_number=int(row["turn_number"]),
        starting_head_sha=str(row["starting_head_sha"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        completed_at=row["completed_at"],
    )


def _validate_session(session_strategy: str, session_id: str | None) -> None:
    if session_strategy not in SESSION_STRATEGIES:
        expected = ", ".join(sorted(SESSION_STRATEGIES))
        raise ValueError(f"session_strategy must be one of: {expected}")
    has_session_id = bool(session_id and session_id.strip())
    if session_strategy == "attached-session" and not has_session_id:
        raise ValueError("attached-session requires session_id")
    if session_strategy != "attached-session" and has_session_id:
        raise ValueError("session_id can only be used with attached-session")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _elapsed_seconds(started_at: str | None) -> int | None:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - started).total_seconds()))


def _worker_status(status: str, active_attempt_id: int | None) -> str:
    if active_attempt_id is not None:
        return "running"
    if status == "unresolved":
        return "watching"
    return status


def _truncate_nullable(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    marker = f"\n[truncated to last {limit} chars]\n"
    return marker + value[-limit:]


def rows_as_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]
