from __future__ import annotations

import json
import os
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

    def get_codex_review_decision(self, pr: PullRequestRef) -> CodexReviewDecision:
        reviews = self._request(f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews")
        comments = self._request(f"/repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments")
        codex_items = _codex_review_items(reviews, comments)
        if not codex_items:
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

    def merge_pr(self, pr: PullRequestRef) -> None:
        self._request(f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/merge", method="PUT", data={})

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(self._api_url + path, data=body, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("User-Agent", "overwatch")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API error {exc.code} for {path}: {body}") from exc

    def _request_optional(self, path: str) -> dict[str, Any]:
        try:
            return self._request(path)
        except RuntimeError as exc:
            return {"error": str(exc)}


def _codex_review_items(reviews: object, comments: object) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for review in reviews if isinstance(reviews, list) else []:
        if _is_codex_authored(review):
            items.append(
                {
                    "body": review.get("body"),
                    "state": review.get("state"),
                    "created_at": review.get("submitted_at") or review.get("created_at") or "",
                }
            )
    for comment in comments if isinstance(comments, list) else []:
        if _is_codex_authored(comment):
            items.append(
                {
                    "body": comment.get("body"),
                    "state": None,
                    "created_at": comment.get("created_at") or "",
                }
            )
    return sorted(items, key=lambda item: str(item["created_at"]))


def _is_codex_authored(item: dict[str, Any]) -> bool:
    user = item.get("user") or {}
    login = str(user.get("login") or "").lower()
    return "codex" in login


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
