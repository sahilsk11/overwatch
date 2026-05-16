from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int
    url: str


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    ref: PullRequestRef
    state: str
    draft: bool
    mergeable: bool | None
    head_sha: str
    base_branch: str
    merged: bool = False


@dataclass(frozen=True, slots=True)
class CiContext:
    source: str
    name: str
    state: str | None = None
    status: str | None = None
    conclusion: str | None = None
    url: str | None = None

    @property
    def is_pending(self) -> bool:
        if self.source == "legacy_status":
            return self.state == "pending"
        return self.status not in {None, "completed"}

    @property
    def is_failing(self) -> bool:
        if self.source == "legacy_status":
            return self.state in {"failure", "error"}
        return self.conclusion in {"failure", "cancelled", "timed_out", "action_required"}


@dataclass(frozen=True, slots=True)
class CiSnapshot:
    head_sha: str
    rollup_state: str
    rollup_contexts: tuple[CiContext, ...] | None = None
    legacy_statuses: tuple[CiContext, ...] | None = None
    check_runs: tuple[CiContext, ...] | None = None
    workflow_runs: tuple[CiContext, ...] | None = None
    source_errors: Mapping[str, str] = field(default_factory=dict)
    details_json: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReviewThread:
    id: str
    author: str
    body: str
    url: str
    path: str | None
    line: int | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ReviewSubmission:
    author: str
    state: str
    body: str
    commit_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class IssueComment:
    author: str
    body: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    reviews: tuple[ReviewSubmission, ...]
    issue_comments: tuple[IssueComment, ...]
    unresolved_threads: tuple[ReviewThread, ...] = ()
    head_sha: str | None = None
