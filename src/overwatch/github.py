from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int
    url: str


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


@dataclass(frozen=True, slots=True)
class ReviewThread:
    id: str
    author: str
    body: str
    url: str
    path: str | None
    line: int | None
    created_at: str


TRUSTED_CODEX_LOGINS = frozenset({"codex", "chatgpt-codex-connector"})


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


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        api_url: str = "https://api.github.com",
    ) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self._api_url = api_url.rstrip("/")

    def get_ci_status(self, pr: PullRequestRef) -> CiStatus:
        pull = self._request(f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}")
        head_sha = str(pull["head"]["sha"])

        combined = self._request(f"/repos/{pr.owner}/{pr.repo}/commits/{head_sha}/status")
        checks = self._request_optional(
            f"/repos/{pr.owner}/{pr.repo}/commits/{head_sha}/check-runs"
        )
        actions = self._request_optional(
            f"/repos/{pr.owner}/{pr.repo}/actions/runs?"
            f"{urllib.parse.urlencode({'head_sha': head_sha})}"
        )

        state = _rollup_state(combined, checks, actions)
        summary = _summarize_status(combined, checks, actions)
        return CiStatus(
            state=state,
            head_sha=head_sha,
            summary=summary,
            details={"combined_status": combined, "check_runs": checks, "actions": actions},
            pr_state=str(pull.get("state", "open")),
            merged=bool(pull.get("merged")),
        )

    def get_codex_review_decision(
        self,
        pr: PullRequestRef,
        head_sha: str | None = None,
    ) -> CodexReviewDecision:
        reviews = self._request_all_pages(f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews")
        comments = self._request_all_pages(
            f"/repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments"
        )
        review_requested_at = _latest_codex_review_request_created_at(comments, head_sha)
        codex_items = _codex_review_items(
            reviews,
            comments,
            head_sha=head_sha,
            min_comment_created_at=review_requested_at,
        )
        if not codex_items:
            if head_sha:
                return CodexReviewDecision(
                    approved=False,
                    summary="No Codex review comments found for the current head.",
                )
            return CodexReviewDecision(approved=False, summary="No Codex review comments found.")

        latest = codex_items[-1]
        state = str(latest.get("state") or "").upper()
        body = str(latest.get("body") or "")
        if state == "APPROVED" or _body_says_codex_approved(body):
            return CodexReviewDecision(approved=True, summary=body or "Codex approved the PR.")
        return CodexReviewDecision(
            approved=False,
            summary=body or "Latest Codex review is not approval.",
        )

    def merge_pr(self, pr: PullRequestRef, head_sha: str | None = None) -> None:
        data = {"sha": head_sha} if head_sha else {}
        self._request(
            f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/merge",
            method="PUT",
            data=data,
        )

    def request_codex_review(self, pr: PullRequestRef, head_sha: str | None = None) -> None:
        body = "@codex review"
        if head_sha:
            body = f"{body}\n\nHead SHA: {head_sha}"
        self._request(
            f"/repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments",
            method="POST",
            data={"body": body},
        )

    def get_unresolved_review_threads(self, pr: PullRequestRef) -> list[ReviewThread]:
        threads: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            data = self._graphql(
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

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        payload, _headers = self._request_page(path, method=method, data=data)
        return payload

    def _request_page(
        self,
        path: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | list[dict[str, Any]], Any]:
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(self._request_url(path), data=body, method=method)
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

    def _request_url(self, path: str) -> str:
        parsed = urllib.parse.urlparse(path)
        if parsed.scheme and parsed.netloc:
            return path
        return self._api_url + path

    def _request_optional(self, path: str) -> dict[str, Any]:
        try:
            return self._request(path)
        except RuntimeError as exc:
            return {"error": str(exc)}

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
) -> str:
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


def _summarize_status(
    combined: dict[str, Any],
    checks: dict[str, Any],
    actions: dict[str, Any],
) -> str:
    lines: list[str] = []
    for status in combined.get("statuses") or []:
        state = status.get("state", "unknown")
        context = status.get("context", "status")
        description = status.get("description") or ""
        lines.append(f"status {context}: {state} {description}".strip())
    for run in checks.get("check_runs") or []:
        name = run.get("name", "check")
        status = run.get("status", "unknown")
        conclusion = run.get("conclusion") or ""
        lines.append(f"check {name}: {status} {conclusion}".strip())
    for run in actions.get("workflow_runs") or []:
        name = run.get("name", "workflow")
        status = run.get("status", "unknown")
        conclusion = run.get("conclusion") or ""
        url = run.get("html_url") or ""
        lines.append(f"workflow {name}: {status} {conclusion} {url}".strip())
    return "\n".join(lines) if lines else "No CI checks reported."
