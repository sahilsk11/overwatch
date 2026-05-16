from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from overwatch.domain import (
    CiContext,
    CiSnapshot,
    PullRequestRef,
    PullRequestSnapshot,
    ReviewSnapshot,
    ReviewThread,
)
from overwatch.policy import CodexApprovalPolicy, CodexReviewParser


@dataclass(frozen=True, slots=True)
class CiStatus:
    state: str
    head_sha: str
    summary: str
    details: dict[str, Any]
    pr_state: str = "open"
    merged: bool = False


@dataclass(frozen=True, slots=True)
class CodexReviewDecision:
    approved: bool
    summary: str


TRUSTED_CODEX_LOGINS = frozenset({"codex", "chatgpt-codex-connector"})
_CODEX_REVIEW_PARSER = CodexReviewParser(TRUSTED_CODEX_LOGINS)
_CODEX_APPROVAL_POLICY = CodexApprovalPolicy(_CODEX_REVIEW_PARSER)


def parse_pr_url(url: str) -> PullRequestRef:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() != "github.com":
        raise ValueError("PR link must be a github.com URL")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 4 or parts[2] != "pull":
        raise ValueError("PR link must look like https://github.com/<owner>/<repo>/pull/<number>")

    try:
        number = int(parts[3])
    except ValueError as exc:
        raise ValueError("PR number must be an integer") from exc

    return PullRequestRef(owner=parts[0], repo=parts[1], number=number, url=url)


class GitHubTransport:
    def __init__(self, token: str | None, api_url: str) -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        payload, _headers = self.request_page(path, method=method, data=data)
        return payload

    def request_page(
        self,
        path: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | list[dict[str, Any]], Any]:
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(self.request_url(path), data=body, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("User-Agent", "overwatch")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8")), response.headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API error {exc.code} for {path}: {body}") from exc

    def request_url(self, path: str) -> str:
        parsed = urllib.parse.urlparse(path)
        if parsed.scheme and parsed.netloc:
            return path
        return self._api_url + path


class PullRequestGateway:
    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    def get_snapshot(self, pr: PullRequestRef) -> PullRequestSnapshot:
        pull = self._client._request(f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}")
        if not isinstance(pull, Mapping):
            raise RuntimeError("GitHub pull request response was not an object")
        head = pull.get("head") or {}
        base = pull.get("base") or {}
        if not isinstance(head, Mapping) or not isinstance(base, Mapping):
            raise RuntimeError("GitHub pull request response did not include head/base refs")
        return PullRequestSnapshot(
            ref=pr,
            state=str(pull.get("state") or "open"),
            draft=bool(pull.get("draft")),
            mergeable=pull.get("mergeable") if isinstance(pull.get("mergeable"), bool) else None,
            head_sha=str(head.get("sha") or ""),
            base_branch=str(base.get("ref") or ""),
            merged=bool(pull.get("merged")),
        )

    def merge(self, pr: PullRequestRef, head_sha: str | None = None) -> None:
        data = {"sha": head_sha} if head_sha else {}
        self._client._request(
            f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/merge",
            method="PUT",
            data=data,
        )

    def create_issue_comment(self, pr: PullRequestRef, body: str) -> None:
        self._client._request(
            f"/repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments",
            method="POST",
            data={"body": body},
        )


class CiGateway:
    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    def get_snapshot(
        self,
        pr: PullRequestRef,
        pr_snapshot: PullRequestSnapshot | None = None,
    ) -> CiSnapshot:
        pr_snapshot = pr_snapshot or self._client.get_pr_snapshot(pr)
        head_sha = pr_snapshot.head_sha
        combined = self._client._request(f"/repos/{pr.owner}/{pr.repo}/commits/{head_sha}/status")
        if not isinstance(combined, Mapping):
            raise RuntimeError("GitHub combined status response was not an object")
        checks = self._client._request_optional(
            f"/repos/{pr.owner}/{pr.repo}/commits/{head_sha}/check-runs"
        )
        actions = self._client._request_optional(
            f"/repos/{pr.owner}/{pr.repo}/actions/runs?"
            f"{urllib.parse.urlencode({'head_sha': head_sha})}"
        )
        rollup = self._status_check_rollup(pr)
        details = {
            "head_sha": head_sha,
            "status_check_rollup": rollup,
            "combined_status": dict(combined),
            "check_runs": checks,
            "actions": actions,
        }
        return CiSnapshot(
            head_sha=head_sha,
            rollup_state=_rollup_state(dict(combined), checks, actions, rollup),
            rollup_contexts=_rollup_contexts(rollup),
            legacy_statuses=_legacy_status_contexts(combined),
            check_runs=_check_run_contexts(checks),
            workflow_runs=_workflow_contexts(actions),
            source_errors=MappingProxyType(_source_errors(details)),
            details_json=MappingProxyType(details),
        )

    def _status_check_rollup(self, pr: PullRequestRef) -> dict[str, Any]:
        try:
            data = self._client._graphql(
                """
            query($owner: String!, $repo: String!, $number: Int!) {
              repository(owner: $owner, name: $repo) {
                pullRequest(number: $number) {
                  commits(last: 1) {
                    nodes {
                      commit {
                        statusCheckRollup {
                          state
                          contexts(first: 100) {
                            nodes {
                              __typename
                              ... on CheckRun {
                                name
                                status
                                conclusion
                                detailsUrl
                              }
                              ... on StatusContext {
                                context
                                state
                                targetUrl
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """,
                {"owner": pr.owner, "repo": pr.repo, "number": pr.number},
            )
        except RuntimeError as exc:
            return {"error": str(exc)}
        nodes = (
            data.get("repository", {})
            .get("pullRequest", {})
            .get("commits", {})
            .get("nodes", [{}])
        )
        if not isinstance(nodes, list) or not nodes:
            return {"contexts": []}
        commit = nodes[-1].get("commit") if isinstance(nodes[-1], Mapping) else {}
        rollup = commit.get("statusCheckRollup") if isinstance(commit, Mapping) else None
        if not isinstance(rollup, Mapping):
            return {"contexts": []}
        contexts = rollup.get("contexts") or {}
        context_nodes = contexts.get("nodes") if isinstance(contexts, Mapping) else []
        return {
            "state": rollup.get("state"),
            "contexts": [node for node in context_nodes if isinstance(node, Mapping)]
            if isinstance(context_nodes, list)
            else [],
        }


class ReviewGateway:
    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    def get_snapshot(self, pr: PullRequestRef, head_sha: str | None = None) -> ReviewSnapshot:
        reviews = self._client._request_all_pages(
            f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews"
        )
        comments = self._client._request_all_pages(
            f"/repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments"
        )
        parsed = _CODEX_REVIEW_PARSER.parse_snapshot(reviews, comments)
        return ReviewSnapshot(
            reviews=parsed.reviews,
            issue_comments=parsed.issue_comments,
            head_sha=head_sha,
        )

    def get_unresolved_review_threads(self, pr: PullRequestRef) -> list[ReviewThread]:
        return _get_unresolved_review_threads(self._client, pr)


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        api_url: str = "https://api.github.com",
    ) -> None:
        token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self._transport = GitHubTransport(token, api_url)
        self.pull_requests = PullRequestGateway(self)
        self.ci = CiGateway(self)
        self.reviews = ReviewGateway(self)

    def get_pr_snapshot(self, pr: PullRequestRef) -> PullRequestSnapshot:
        return self.pull_requests.get_snapshot(pr)

    def get_ci_snapshot(
        self,
        pr: PullRequestRef,
        pr_snapshot: PullRequestSnapshot | None = None,
    ) -> CiSnapshot:
        return self.ci.get_snapshot(pr, pr_snapshot)

    def get_review_snapshot(
        self,
        pr: PullRequestRef,
        head_sha: str | None = None,
    ) -> ReviewSnapshot:
        return self.reviews.get_snapshot(pr, head_sha)

    def get_ci_status(self, pr: PullRequestRef) -> CiStatus:
        pr_snapshot = self.get_pr_snapshot(pr)
        ci_snapshot = self.get_ci_snapshot(pr, pr_snapshot)
        return ci_status_from_snapshots(pr_snapshot, ci_snapshot)

    def get_codex_review_decision(
        self,
        pr: PullRequestRef,
        head_sha: str | None = None,
    ) -> CodexReviewDecision:
        decision = _CODEX_APPROVAL_POLICY.evaluate(self.get_review_snapshot(pr, head_sha), head_sha)
        return CodexReviewDecision(approved=decision.approved, summary=decision.summary)

    def merge_pr(self, pr: PullRequestRef, head_sha: str | None = None) -> None:
        self.pull_requests.merge(pr, head_sha)

    def request_codex_review(self, pr: PullRequestRef, head_sha: str | None = None) -> None:
        body = "@codex review"
        if head_sha:
            body = f"{body}\n\nHead SHA: {head_sha}"
        self.pull_requests.create_issue_comment(pr, body)

    def get_unresolved_review_threads(self, pr: PullRequestRef) -> list[ReviewThread]:
        return self.reviews.get_unresolved_review_threads(pr)

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        return self._transport.request(path, method=method, data=data)

    def _request_page(
        self,
        path: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | list[dict[str, Any]], Any]:
        return self._transport.request_page(path, method=method, data=data)

    def _request_url(self, path: str) -> str:
        return self._transport.request_url(path)

    def _request_optional(self, path: str) -> dict[str, Any]:
        try:
            response = self._request(path)
        except RuntimeError as exc:
            return {"error": str(exc)}
        if not isinstance(response, dict):
            return {"error": "GitHub response was not an object"}
        return response

    def _request_all_pages(self, path: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        next_path = path
        while next_path:
            response, headers = self._request_page(next_path)
            if isinstance(response, list):
                results.extend(response)
            else:
                results.append(response)
            next_path = self._next_page_link(headers.get("Link"))
        return results

    def _next_page_link(self, link_header: str | None) -> str | None:
        if not link_header:
            return None
        for part in link_header.split(","):
            if 'rel="next"' in part:
                match = re.search(r'<([^>]+)>', part)
                if match:
                    return match.group(1)
        return None

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "/graphql",
            method="POST",
            data={"query": query, "variables": variables},
        )
        if not isinstance(response, dict):
            raise RuntimeError("GitHub GraphQL response was not an object")
        if response.get("errors"):
            raise RuntimeError(f"GitHub GraphQL error: {response['errors']}")
        data = response.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("GitHub GraphQL response did not include data")
        return data


def ci_status_from_snapshots(pr: PullRequestSnapshot, ci: CiSnapshot) -> CiStatus:
    return CiStatus(
        state=ci.rollup_state,
        head_sha=ci.head_sha,
        summary=_summarize_ci_snapshot(ci),
        details=dict(ci.details_json),
        pr_state=pr.state,
        merged=pr.merged,
    )


def _get_unresolved_review_threads(client: GitHubClient, pr: PullRequestRef) -> list[ReviewThread]:
    threads: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        data = client._graphql(
            """
        query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              reviewThreads(first: 100, after: $cursor) {
                pageInfo {
                  hasNextPage
                  endCursor
                }
                nodes {
                  id
                  isResolved
                  isOutdated
                  path
                  line
                  comments(last: 1) {
                    nodes {
                      author { login }
                      body
                      url
                      createdAt
                    }
                  }
                }
              }
            }
          }
        }
        """,
            {"owner": pr.owner, "repo": pr.repo, "number": pr.number, "cursor": cursor},
        )
        review_threads = (
            data.get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
        )
        if not isinstance(review_threads, dict):
            raise RuntimeError("GitHub GraphQL response did not include reviewThreads")
        nodes = review_threads.get("nodes") or []
        if isinstance(nodes, list):
            threads.extend(node for node in nodes if isinstance(node, dict))
        page_info = review_threads.get("pageInfo") or {}
        if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
            break
        cursor = str(page_info.get("endCursor") or "")
        if not cursor:
            raise RuntimeError("GitHub GraphQL reviewThreads page did not include endCursor")
    return _unresolved_review_threads(threads)


def _unresolved_review_threads(threads: object) -> list[ReviewThread]:
    unresolved: list[ReviewThread] = []
    for thread in threads if isinstance(threads, list) else []:
        if not isinstance(thread, dict) or thread.get("isResolved"):
            continue
        comments = thread.get("comments") or {}
        nodes = comments.get("nodes") if isinstance(comments, dict) else []
        if not isinstance(nodes, list) or not nodes:
            continue
        comment = nodes[-1]
        if not isinstance(comment, dict):
            continue
        author = comment.get("author") or {}
        unresolved.append(
            ReviewThread(
                id=str(thread.get("id") or ""),
                author=str(author.get("login") or "unknown"),
                body=str(comment.get("body") or ""),
                url=str(comment.get("url") or ""),
                path=str(thread["path"]) if thread.get("path") else None,
                line=int(thread["line"]) if thread.get("line") else None,
                created_at=str(comment.get("createdAt") or ""),
            )
        )
    return unresolved


def _codex_review_items(
    reviews: object,
    comments: object,
    *,
    head_sha: str | None = None,
    min_comment_created_at: str | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for review in reviews if isinstance(reviews, list) else []:
        if _is_codex_authored(review) and _matches_head(review, head_sha):
            items.append(
                {
                    "body": review.get("body"),
                    "state": review.get("state"),
                    "commit_id": review.get("commit_id"),
                    "created_at": review.get("submitted_at") or review.get("created_at") or "",
                }
            )
    for comment in comments if isinstance(comments, list) else []:
        if _is_codex_authored(comment) and _comment_matches_head_window(
            comment,
            head_sha,
            min_comment_created_at,
        ):
            items.append(
                {
                    "body": comment.get("body"),
                    "state": None,
                    "created_at": comment.get("created_at") or "",
                }
            )
    return sorted(items, key=lambda item: str(item["created_at"]))


def _latest_codex_review_request_created_at(comments: object, head_sha: str | None) -> str | None:
    head_timestamps: list[str] = []
    fallback_timestamps: list[str] = []
    for comment in comments if isinstance(comments, list) else []:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "").lower()
        if "@codex review" not in body:
            continue
        created_at = str(comment.get("created_at") or "")
        if not created_at:
            continue
        fallback_timestamps.append(created_at)
        if head_sha is not None and head_sha.lower() in body:
            head_timestamps.append(created_at)
    if head_sha is not None:
        return max(head_timestamps, default=None)
    return max(fallback_timestamps, default=None)


def _comment_matches_head_window(
    item: dict[str, Any],
    head_sha: str | None,
    min_created_at: str | None,
) -> bool:
    if head_sha is None:
        return True
    if min_created_at is None:
        return False
    return str(item.get("created_at") or "") >= min_created_at


def _matches_head(item: dict[str, Any], head_sha: str | None) -> bool:
    if head_sha is None:
        return True
    return item.get("commit_id") == head_sha


def _is_codex_authored(item: dict[str, Any]) -> bool:
    user = item.get("user") or {}
    login = str(user.get("login") or "").lower().removesuffix("[bot]")
    return login in TRUSTED_CODEX_LOGINS


def _body_says_codex_approved(body: str) -> bool:
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


def _rollup_state(
    combined: dict[str, Any],
    checks: dict[str, Any],
    actions: dict[str, Any],
    status_check_rollup: dict[str, Any] | None = None,
) -> str:
    rollup_contexts = _rollup_contexts(status_check_rollup or {})
    if rollup_contexts:
        rollup_state = str((status_check_rollup or {}).get("state") or "").lower()
        if rollup_state in {"success", "failure", "error", "pending", "expected"}:
            return _normalize_rollup_state(rollup_state)
        if any(context.is_failing for context in rollup_contexts):
            return "failure"
        if any(context.is_pending for context in rollup_contexts):
            return "pending"
        return "success"

    check_runs = checks.get("check_runs") or []
    conclusions = {run.get("conclusion") for run in check_runs if run.get("status") == "completed"}
    in_progress = any(run.get("status") != "completed" for run in check_runs)
    workflow_runs = actions.get("workflow_runs") or []
    workflow_conclusions = {
        run.get("conclusion") for run in workflow_runs if run.get("status") == "completed"
    }
    workflows_in_progress = any(run.get("status") != "completed" for run in workflow_runs)
    statuses = combined.get("statuses") or []
    legacy_status_pending = bool(statuses) and combined.get("state") == "pending"

    failing_conclusions = {"failure", "cancelled", "timed_out", "action_required"}
    if (conclusions | workflow_conclusions) & failing_conclusions:
        return "failure"
    if combined.get("state") in {"failure", "error"}:
        return "failure"
    if in_progress or workflows_in_progress or legacy_status_pending:
        return "pending"
    if check_runs or workflow_runs or statuses:
        return "success"
    return "unknown"


def _normalize_rollup_state(state: str) -> str:
    if state == "success":
        return "success"
    if state in {"failure", "error"}:
        return "failure"
    return "pending"


def _summarize_status(
    combined: dict[str, Any],
    checks: dict[str, Any],
    actions: dict[str, Any],
) -> str:
    return _summarize_ci_contexts(
        _legacy_status_contexts(combined) or (),
        _check_run_contexts(checks) or (),
        _workflow_contexts(actions) or (),
    )


def _summarize_ci_snapshot(snapshot: CiSnapshot) -> str:
    return _summarize_ci_contexts(
        snapshot.rollup_contexts or (),
        snapshot.legacy_statuses or (),
        snapshot.check_runs or (),
        snapshot.workflow_runs or (),
    )


def _summarize_ci_contexts(*groups: tuple[CiContext, ...]) -> str:
    lines: list[str] = []
    for context in tuple(item for group in groups for item in group):
        label = {
            "legacy_status": "status",
            "check_run": "check",
            "workflow_run": "workflow",
            "rollup": "rollup",
        }.get(context.source, context.source)
        status = context.state or context.status or "unknown"
        conclusion = context.conclusion or ""
        url = context.url or ""
        lines.append(f"{label} {context.name}: {status} {conclusion} {url}".strip())
    return "\n".join(lines) if lines else "No CI checks reported."


def _rollup_contexts(source: Mapping[str, Any] | None) -> tuple[CiContext, ...] | None:
    if not isinstance(source, Mapping) or source.get("error"):
        return None
    contexts = source.get("contexts")
    if not isinstance(contexts, list):
        return None
    parsed: list[CiContext] = []
    for context in contexts:
        if not isinstance(context, Mapping):
            continue
        parsed.append(
            CiContext(
                source="rollup",
                name=str(
                    context.get("context")
                    or context.get("name")
                    or context.get("__typename")
                    or "check"
                ),
                state=_lower(context.get("state")),
                status=_lower(context.get("status")),
                conclusion=_lower(context.get("conclusion")),
                url=_optional_str(context.get("targetUrl") or context.get("detailsUrl")),
            )
        )
    return tuple(parsed)


def _legacy_status_contexts(source: Mapping[str, Any]) -> tuple[CiContext, ...] | None:
    statuses = source.get("statuses")
    if not isinstance(statuses, list):
        return None
    return tuple(
        CiContext(
            source="legacy_status",
            name=str(status.get("context") or "status"),
            state=_lower(status.get("state")),
            url=_optional_str(status.get("target_url")),
        )
        for status in statuses
        if isinstance(status, Mapping)
    )


def _check_run_contexts(source: Mapping[str, Any]) -> tuple[CiContext, ...] | None:
    if source.get("error"):
        return None
    runs = source.get("check_runs")
    if not isinstance(runs, list):
        return None
    return tuple(
        CiContext(
            source="check_run",
            name=str(run.get("name") or "check"),
            status=_lower(run.get("status")),
            conclusion=_lower(run.get("conclusion")),
            url=_optional_str(run.get("html_url") or run.get("details_url")),
        )
        for run in runs
        if isinstance(run, Mapping)
    )


def _workflow_contexts(source: Mapping[str, Any]) -> tuple[CiContext, ...] | None:
    if source.get("error"):
        return None
    runs = source.get("workflow_runs")
    if not isinstance(runs, list):
        return None
    return tuple(
        CiContext(
            source="workflow_run",
            name=str(run.get("name") or "workflow"),
            status=_lower(run.get("status")),
            conclusion=_lower(run.get("conclusion")),
            url=_optional_str(run.get("html_url")),
        )
        for run in runs
        if isinstance(run, Mapping)
    )


def _source_errors(details: Mapping[str, object]) -> dict[str, str]:
    return {
        name: str(source.get("error"))
        for name, source in details.items()
        if isinstance(source, Mapping) and source.get("error")
    }


def _lower(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text.lower() if text else None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
