from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from overwatch.store import (
    DONE_STATUSES,
    Store,
    WatchedPullRequest,
    WatchedPullRequestSummary,
    default_db_path,
)

PrBucket = Literal["active", "done", "all"]

class HealthResponse(BaseModel):
    status: Literal["ok"]


class PrResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    latest_ci_state: str | None = None
    latest_head_sha: str | None = None
    latest_summary: str | None = None
    latest_checked_at: str | None = None
    worker_status: str | None = None
    active_attempt_id: int | None = None
    active_attempt_started_at: str | None = None
    active_attempt_elapsed_seconds: int | None = None
    active_attempt_status: str | None = None
    last_attempt_status: str | None = None
    last_attempt_completed_at: str | None = None
    last_provider_command: str | None = None
    last_provider_output: str | None = None
    last_error: str | None = None


class PrSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class CiHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    head_sha: str
    state: str
    summary: str
    details: dict[str, Any]
    created_at: str


class ResolutionAttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class PrEventsResponse(BaseModel):
    ci_history: list[CiHistoryResponse]
    resolution_attempts: list[ResolutionAttemptResponse]


def default_static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


def configured_static_dir() -> Path:
    configured = os.environ.get("OVERWATCH_STATIC_DIR")
    return Path(configured).expanduser() if configured else default_static_dir()


def configured_db_path() -> Path:
    configured = os.environ.get("OVERWATCH_DB")
    return Path(configured).expanduser() if configured else default_db_path()


def _store(app: FastAPI) -> Store:
    return app.state.store


def _store_dependency(request: Request) -> Store:
    return request.app.state.store


StoreDependency = Annotated[Store, Depends(_store_dependency)]


def create_app(
    *,
    store: Store | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _store(app).init()
        yield

    app = FastAPI(title="Overwatch API", lifespan=lifespan)
    app.state.store = store or Store(configured_db_path())
    app.state.static_dir = static_dir or configured_static_dir()

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/api/prs", response_model=list[PrSummaryResponse])
    def list_prs(
        store: StoreDependency,
        bucket: Annotated[PrBucket, Query()] = "active",
    ) -> list[WatchedPullRequestSummary]:
        rows = store.watched_prs(include_inactive=True)
        if bucket == "active":
            return [row for row in rows if row.status not in DONE_STATUSES]
        if bucket == "done":
            return [row for row in rows if row.status in DONE_STATUSES]
        return rows

    @app.get("/api/prs/{pr_id}", response_model=PrResponse)
    def get_pr(pr_id: int, store: StoreDependency) -> PrResponse:
        return _pr_response_or_404(store, pr_id)

    @app.get("/api/prs/{pr_id}/events", response_model=PrEventsResponse)
    def get_pr_events(pr_id: int, store: StoreDependency) -> PrEventsResponse:
        _get_pr_or_404(store, pr_id)
        ci_history, attempts = store.pr_events(pr_id)
        return PrEventsResponse(
            ci_history=[CiHistoryResponse.model_validate(event) for event in ci_history],
            resolution_attempts=[
                ResolutionAttemptResponse.model_validate(attempt) for attempt in attempts
            ],
        )

    @app.get("/{path:path}", include_in_schema=False)
    def serve_spa(path: str, request: Request) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return _static_response(request.app.state.static_dir, path)

    return app

def _get_pr_or_404(store: Store, pr_id: int) -> WatchedPullRequest:
    watched = store.get_pr(pr_id)
    if watched is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PR not found")
    return watched


def _pr_response_or_404(store: Store, pr_id: int) -> PrResponse:
    watched = _get_pr_or_404(store, pr_id)
    latest = next(
        (row for row in store.watched_prs(include_inactive=True) if row.id == pr_id),
        None,
    )
    payload = PrResponse.model_validate(watched).model_dump()
    if latest is not None:
        payload.update(
            {
                "latest_ci_state": latest.latest_ci_state,
                "latest_head_sha": latest.latest_head_sha,
                "latest_summary": latest.latest_summary,
                "latest_checked_at": latest.latest_checked_at,
                "worker_status": latest.worker_status,
                "active_attempt_id": latest.active_attempt_id,
                "active_attempt_started_at": latest.active_attempt_started_at,
                "active_attempt_elapsed_seconds": latest.active_attempt_elapsed_seconds,
                "active_attempt_status": latest.active_attempt_status,
                "last_attempt_status": latest.last_attempt_status,
                "last_attempt_completed_at": latest.last_attempt_completed_at,
                "last_provider_command": latest.last_provider_command,
                "last_provider_output": latest.last_provider_output,
                "last_error": latest.last_error,
            }
        )
    return PrResponse.model_validate(payload)


def _static_response(static_dir: Path, path: str) -> FileResponse:
    root = static_dir.resolve()
    index = root / "index.html"
    candidate = (root / path).resolve()
    if _is_relative_to(candidate, root) and candidate.is_file():
        return FileResponse(candidate)
    if index.is_file():
        return FileResponse(index)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Static assets not found")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


app = create_app()
