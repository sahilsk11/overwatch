from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from overwatch.domain import CiContext, CiSnapshot, IssueComment, ReviewSnapshot, ReviewSubmission

TRUSTED_CODEX_LOGINS = frozenset({"codex", "chatgpt-codex-connector"})


@dataclass(frozen=True, slots=True)
class CiDecision:
    state: str
    summary: str

    @property
    def allows_bot_merge(self) -> bool:
        return self.state in {"passing", "no_checks"}

    @property
    def should_autofix(self) -> bool:
        return self.state == "failing"


class CiPolicy:
    @classmethod
    def classify(cls, snapshot: CiSnapshot) -> CiDecision:
        if snapshot.rollup_state == "success":
            return CiDecision("passing", "CI is passing.")
        if snapshot.rollup_state == "failure":
            return CiDecision("failing", "CI is failing.")
        if snapshot.rollup_state == "pending":
            return CiDecision("pending", "CI is pending.")
        if cls._has_no_checks(snapshot):
            return CiDecision("no_checks", "No CI checks are configured.")
        return CiDecision("incomplete", "CI status data is incomplete.")

    @classmethod
    def snapshot_from_status(cls, state: str, details: Mapping[str, Any]) -> CiSnapshot:
        rollup_contexts = cls._contexts(details.get("status_check_rollup"), "contexts")
        legacy_statuses = cls._contexts(details.get("combined_status"), "statuses")
        check_runs = cls._contexts(details.get("check_runs"), "check_runs")
        workflow_runs = cls._contexts(details.get("actions"), "workflow_runs")
        errors = {
            name: str(source.get("error"))
            for name, source in (
                ("status_check_rollup", details.get("status_check_rollup")),
                ("combined_status", details.get("combined_status")),
                ("check_runs", details.get("check_runs")),
                ("actions", details.get("actions")),
            )
            if isinstance(source, Mapping) and source.get("error")
        }
        return CiSnapshot(
            head_sha=str(details.get("head_sha") or ""),
            rollup_contexts=rollup_contexts,
            legacy_statuses=legacy_statuses,
            check_runs=check_runs,
            workflow_runs=workflow_runs,
            source_errors=errors,
            rollup_state=state,
        )

    @classmethod
    def _has_no_checks(cls, snapshot: CiSnapshot) -> bool:
        if cls._is_empty(snapshot.rollup_contexts):
            return True
        if all(
            cls._is_empty(contexts)
            for contexts in (
                snapshot.legacy_statuses,
                snapshot.check_runs,
                snapshot.workflow_runs,
            )
        ):
            return True
        return (
            cls._is_empty(snapshot.legacy_statuses)
            and cls._is_empty(snapshot.workflow_runs)
            and snapshot.check_runs is None
            and cls._is_check_runs_visibility_error(snapshot.source_errors.get("check_runs"))
        )

    @staticmethod
    def _is_empty(contexts: tuple[CiContext, ...] | None) -> bool:
        return contexts is not None and not contexts

    @staticmethod
    def _contexts(source: object, key: str) -> tuple[CiContext, ...] | None:
        if not isinstance(source, Mapping) or source.get("error"):
            return None
        value = source.get(key)
        if not isinstance(value, list):
            return None
        return tuple(
            _ci_context_from_mapping(key, item) for item in value if isinstance(item, Mapping)
        )

    @staticmethod
    def _is_check_runs_visibility_error(error: str | None) -> bool:
        if not error:
            return False
        text = error.lower()
        return "403" in text or "404" in text or "resource not accessible" in text


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approved: bool
    summary: str


class CodexReviewParser:
    def __init__(self, trusted_logins: frozenset[str] = TRUSTED_CODEX_LOGINS) -> None:
        self._trusted_logins = trusted_logins

    def parse_snapshot(self, reviews: object, comments: object) -> ReviewSnapshot:
        return ReviewSnapshot(
            reviews=tuple(self._parse_reviews(reviews)),
            issue_comments=tuple(self._parse_issue_comments(comments)),
        )

    def is_codex_authored(self, item: Mapping[str, Any]) -> bool:
        return self.is_trusted_author(self._author_login(item))

    def is_trusted_author(self, login: str) -> bool:
        normalized = login.lower().removesuffix("[bot]")
        return normalized in self._trusted_logins

    def body_says_approved(self, body: str) -> bool:
        text = " ".join(body.lower().split())
        if not text:
            return False
        if "codex review" not in text and "codex" not in text:
            return False
        if "major issues" not in text:
            return False
        return any(
            phrase in text
            for phrase in (
                "didn't find any major issues",
                "did not find any major issues",
                "no major issues",
            )
        )

    def latest_review_request_created_at(
        self,
        comments: Sequence[IssueComment],
        head_sha: str | None,
    ) -> str | None:
        head_timestamps: list[str] = []
        fallback_timestamps: list[str] = []
        for comment in comments:
            if "@codex review" not in comment.body.lower():
                continue
            if not comment.created_at:
                continue
            fallback_timestamps.append(comment.created_at)
            if head_sha is not None and head_sha.lower() in comment.body.lower():
                head_timestamps.append(comment.created_at)
        if head_sha is not None:
            return max(head_timestamps, default=None)
        return max(fallback_timestamps, default=None)

    def _parse_reviews(self, reviews: object) -> list[ReviewSubmission]:
        parsed: list[ReviewSubmission] = []
        for review in reviews if isinstance(reviews, list) else []:
            if not isinstance(review, Mapping):
                continue
            parsed.append(
                ReviewSubmission(
                    author=self._author_login(review),
                    state=str(review.get("state") or ""),
                    body=str(review.get("body") or ""),
                    commit_id=str(review["commit_id"]) if review.get("commit_id") else None,
                    created_at=str(review.get("submitted_at") or review.get("created_at") or ""),
                )
            )
        return parsed

    def _parse_issue_comments(self, comments: object) -> list[IssueComment]:
        parsed: list[IssueComment] = []
        for comment in comments if isinstance(comments, list) else []:
            if not isinstance(comment, Mapping):
                continue
            parsed.append(
                IssueComment(
                    author=self._author_login(comment),
                    body=str(comment.get("body") or ""),
                    created_at=str(comment.get("created_at") or ""),
                )
            )
        return parsed

    @staticmethod
    def _author_login(item: Mapping[str, Any]) -> str:
        user = item.get("user") or item.get("author") or {}
        if not isinstance(user, Mapping):
            return ""
        return str(user.get("login") or "")


class CodexApprovalPolicy:
    def __init__(self, parser: CodexReviewParser | None = None) -> None:
        self._parser = parser or CodexReviewParser()

    def evaluate(self, snapshot: ReviewSnapshot, head_sha: str | None = None) -> ApprovalDecision:
        items: list[_ApprovalItem] = []
        items.extend(self._formal_review_items(snapshot.reviews, head_sha))
        request_created_at = self._parser.latest_review_request_created_at(
            snapshot.issue_comments,
            head_sha,
        )
        items.extend(
            self._issue_comment_items(
                snapshot.issue_comments,
                request_created_at,
            )
        )
        if not items:
            if head_sha:
                return ApprovalDecision(
                    approved=False,
                    summary="No Codex review comments found for the current head.",
                )
            return ApprovalDecision(approved=False, summary="No Codex review comments found.")

        latest = sorted(items, key=lambda item: item.created_at)[-1]
        if latest.approved:
            return ApprovalDecision(approved=True, summary=latest.summary)
        return ApprovalDecision(approved=False, summary=latest.summary)

    def _formal_review_items(
        self,
        reviews: Sequence[ReviewSubmission],
        head_sha: str | None,
    ) -> list[_ApprovalItem]:
        items: list[_ApprovalItem] = []
        for review in reviews:
            if not self._parser.is_trusted_author(review.author):
                continue
            if head_sha is not None and review.commit_id != head_sha:
                continue
            body_approved = self._parser.body_says_approved(review.body)
            approved = review.state.upper() == "APPROVED" or body_approved
            items.append(
                _ApprovalItem(
                    created_at=review.created_at,
                    approved=approved,
                    summary=review.body or self._default_review_summary(approved),
                )
            )
        return items

    def _issue_comment_items(
        self,
        comments: Sequence[IssueComment],
        min_created_at: str | None,
    ) -> list[_ApprovalItem]:
        if min_created_at is None:
            return []
        items: list[_ApprovalItem] = []
        for comment in comments:
            if not self._parser.is_trusted_author(comment.author):
                continue
            if comment.created_at < min_created_at:
                continue
            if not self._parser.body_says_approved(comment.body):
                continue
            items.append(
                _ApprovalItem(
                    created_at=comment.created_at,
                    approved=True,
                    summary=comment.body or "Codex approved the PR.",
                )
            )
        return items

    @staticmethod
    def _default_review_summary(approved: bool) -> str:
        if approved:
            return "Codex approved the PR."
        return "Latest Codex review is not approval."


@dataclass(frozen=True, slots=True)
class WatchPolicy:
    autofix: bool
    merge_on_bot_approval: bool


@dataclass(frozen=True, slots=True)
class MergeDecision:
    can_merge: bool
    reason: str


class MergePolicy:
    def evaluate(
        self,
        *,
        ci: CiDecision,
        approval: ApprovalDecision | None,
        watch_policy: WatchPolicy,
        unresolved_review_threads: Sequence[object] = (),
    ) -> MergeDecision:
        if not watch_policy.merge_on_bot_approval:
            return MergeDecision(False, "merge-on-bot-approval is disabled")
        if unresolved_review_threads:
            return MergeDecision(False, "unresolved review threads block merge")
        if not ci.allows_bot_merge:
            return MergeDecision(False, f"CI state {ci.state} blocks merge")
        if approval is None or not approval.approved:
            return MergeDecision(False, "Codex approval is required")
        return MergeDecision(True, "CI and Codex approval allow merge")


def _ci_context_from_mapping(source_key: str, item: Mapping[str, Any]) -> CiContext:
    source = {
        "statuses": "legacy_status",
        "check_runs": "check_run",
        "workflow_runs": "workflow_run",
        "contexts": "rollup",
    }.get(source_key, source_key)
    name = str(
        item.get("context")
        or item.get("name")
        or item.get("workflowName")
        or item.get("__typename")
        or source
    )
    state = _normalized_lower(item.get("state"))
    status = _normalized_lower(item.get("status"))
    conclusion = _normalized_lower(item.get("conclusion"))
    url = item.get("target_url") or item.get("html_url") or item.get("url")
    return CiContext(
        source=source,
        name=name,
        state=state,
        status=status,
        conclusion=conclusion,
        url=str(url) if url else None,
    )


def _normalized_lower(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text.lower() if text else None


@dataclass(frozen=True, slots=True)
class _ApprovalItem:
    created_at: str
    approved: bool
    summary: str
