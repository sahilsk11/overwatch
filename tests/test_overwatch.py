from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from overwatch.api import create_app
from overwatch.cli import _print_watched_prs
from overwatch.github import (
    CiStatus,
    CodexReviewDecision,
    GitHubClient,
    PullRequestRef,
    ReviewThread,
    _body_says_codex_approved,
    _is_codex_authored,
    _rollup_state,
    _summarize_status,
    _unresolved_review_threads,
    parse_pr_url,
)
from overwatch.providers import AgentConfig, ProviderRegistry
from overwatch.store import Store
from overwatch.worker import run_once


class FakeGitHubClient:
    def __init__(
        self,
        status: CiStatus,
        decision: CodexReviewDecision | None = None,
        review_threads: list[ReviewThread] | None = None,
        review_threads_error: RuntimeError | None = None,
    ) -> None:
        self.status = status
        self.decision = decision or CodexReviewDecision(approved=False, summary="No approval")
        self.review_threads = review_threads or []
        self.review_threads_error = review_threads_error
        self.checked: list[PullRequestRef] = []
        self.reviewed: list[PullRequestRef] = []
        self.review_head_shas: list[str | None] = []
        self.thread_checked: list[PullRequestRef] = []
        self.merged: list[PullRequestRef] = []
        self.merge_head_shas: list[str | None] = []
        self.review_requests: list[PullRequestRef] = []

    def get_ci_status(self, pr: PullRequestRef) -> CiStatus:
        self.checked.append(pr)
        return self.status

    def get_codex_review_decision(
        self,
        pr: PullRequestRef,
        head_sha: str | None = None,
    ) -> CodexReviewDecision:
        self.reviewed.append(pr)
        self.review_head_shas.append(head_sha)
        return self.decision

    def get_unresolved_review_threads(self, pr: PullRequestRef) -> list[ReviewThread]:
        self.thread_checked.append(pr)
        if self.review_threads_error:
            raise self.review_threads_error
        return self.review_threads

    def merge_pr(self, pr: PullRequestRef, head_sha: str | None = None) -> None:
        self.merged.append(pr)
        self.merge_head_shas.append(head_sha)

    def request_codex_review(self, pr: PullRequestRef, head_sha: str | None = None) -> None:
        self.review_requests.append(pr)


class FakeProvider:
    provider_id = "opencode"

    def __init__(self) -> None:
        self.calls: list[tuple[str, AgentConfig]] = []

    async def run(self, prompt: str, config: AgentConfig) -> None:
        self.calls.append((prompt, config))


class ParsePrUrlTest(unittest.TestCase):
    def test_parses_github_pr_url(self) -> None:
        pr = parse_pr_url("https://github.com/example/repo/pull/42")

        self.assertEqual(pr.owner, "example")
        self.assertEqual(pr.repo, "repo")
        self.assertEqual(pr.number, 42)

    def test_rejects_non_pr_url(self) -> None:
        with self.assertRaises(ValueError):
            parse_pr_url("https://github.com/example/repo/issues/42")


class CiStatusTest(unittest.TestCase):
    def test_actions_failure_marks_ci_failed(self) -> None:
        combined = {"state": "pending", "statuses": []}
        checks = {"error": "check-runs unavailable"}
        actions = {
            "workflow_runs": [
                {
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/example/repo/actions/runs/1",
                }
            ]
        }

        self.assertEqual(_rollup_state(combined, checks, actions), "failure")
        summary = _summarize_status(combined, checks, actions)

        self.assertIn("workflow CI: completed failure", summary)

    def test_actions_success_ignores_empty_legacy_status_pending(self) -> None:
        combined = {"state": "pending", "statuses": []}
        checks = {"error": "check-runs unavailable"}
        actions = {
            "workflow_runs": [
                {
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        }

        self.assertEqual(_rollup_state(combined, checks, actions), "success")

    def test_codex_approval_text_is_detected(self) -> None:
        self.assertTrue(
            _body_says_codex_approved(
                "Codex review: didn't find any major issues. Keep it up."
            )
        )

    def test_codex_actionable_feedback_is_not_approval(self) -> None:
        self.assertFalse(_body_says_codex_approved("Codex review: please fix the failing test."))

    def test_unresolved_review_threads_ignores_resolved_threads(self) -> None:
        threads = _unresolved_review_threads(
            [
                {
                    "id": "THREAD1",
                    "isResolved": True,
                    "path": "src/example.py",
                    "line": 10,
                    "comments": {
                        "nodes": [
                            {
                                "author": {"login": "codex"},
                                "body": "Resolved comment",
                                "url": "https://github.com/example/repo/pull/1#discussion_r1",
                                "createdAt": "2026-05-15T00:00:00Z",
                            }
                        ]
                    },
                },
                {
                    "id": "THREAD2",
                    "isResolved": False,
                    "path": "src/example.py",
                    "line": 12,
                    "comments": {
                        "nodes": [
                            {
                                "author": {"login": "codex"},
                                "body": "Please remove this smoke test.",
                                "url": "https://github.com/example/repo/pull/1#discussion_r2",
                                "createdAt": "2026-05-15T00:01:00Z",
                            }
                        ]
                    },
                },
            ]
        )

        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].id, "THREAD2")
        self.assertEqual(threads[0].author, "codex")
        self.assertEqual(threads[0].body, "Please remove this smoke test.")


class GitHubClientTest(unittest.TestCase):
    def test_request_all_pages_follows_link_header(self) -> None:
        class PagingClient(GitHubClient):
            def __init__(self) -> None:
                super().__init__(token="token", api_url="https://api.github.test")
                self.calls: list[str] = []

            def _request_page(
                self,
                path: str,
                *,
                method: str = "GET",
                data: dict[str, object] | None = None,
            ) -> tuple[list[dict[str, object]], dict[str, str]]:
                self.calls.append(path)
                if path == "/first":
                    return (
                        [{"id": 1}],
                        {"Link": '<https://api.github.test/second>; rel="next"'},
                    )
                if path == "https://api.github.test/second":
                    return ([{"id": 2}], {})
                raise AssertionError(f"unexpected path: {path}")

        client = PagingClient()

        self.assertEqual(client._request_all_pages("/first"), [{"id": 1}, {"id": 2}])
        self.assertEqual(client.calls, ["/first", "https://api.github.test/second"])

    def test_get_unresolved_review_threads_follows_graphql_pages(self) -> None:
        class ThreadPagingClient(GitHubClient):
            def __init__(self) -> None:
                super().__init__(token="token", api_url="https://api.github.test")
                self.cursors: list[str | None] = []

            def _graphql(self, query: str, variables: dict[str, object]) -> dict[str, object]:
                self.cursors.append(variables.get("cursor"))
                if variables.get("cursor") is None:
                    return {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR2"},
                                    "nodes": [
                                        {
                                            "id": "THREAD1",
                                            "isResolved": True,
                                            "comments": {"nodes": []},
                                        }
                                    ],
                                }
                            }
                        }
                    }
                return {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "THREAD2",
                                        "isResolved": False,
                                        "path": "src/example.py",
                                        "line": 12,
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "author": {"login": "codex"},
                                                    "body": "Please fix this.",
                                                    "url": "https://example.test/thread2",
                                                    "createdAt": "2026-05-15T00:01:00Z",
                                                }
                                            ]
                                        },
                                    }
                                ],
                            }
                        }
                    }
                }

        client = ThreadPagingClient()
        threads = client.get_unresolved_review_threads(
            PullRequestRef("example", "repo", 42, "https://github.com/example/repo/pull/42")
        )

        self.assertEqual(client.cursors, [None, "CURSOR2"])
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].id, "THREAD2")

    def test_codex_review_decision_requires_current_head_review(self) -> None:
        class ReviewClient(GitHubClient):
            def _request_all_pages(self, path: str) -> list[dict[str, object]]:
                if path.endswith("/reviews"):
                    return [
                        {
                            "user": {"login": "codex"},
                            "state": "APPROVED",
                            "commit_id": "old-sha",
                            "body": "Looks good",
                            "submitted_at": "2026-05-15T00:00:00Z",
                        },
                        {
                            "user": {"login": "codex"},
                            "state": "COMMENTED",
                            "commit_id": "current-sha",
                            "body": "Please fix this.",
                            "submitted_at": "2026-05-15T00:01:00Z",
                        },
                    ]
                return [
                    {
                        "user": {"login": "codex"},
                        "body": "Codex review: didn't find any major issues.",
                        "created_at": "2026-05-15T00:00:01Z",
                    }
                ]

        client = ReviewClient(token="token")

        decision = client.get_codex_review_decision(
            PullRequestRef("example", "repo", 42, "https://github.com/example/repo/pull/42"),
            "current-sha",
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.summary, "Please fix this.")

    def test_codex_review_decision_accepts_current_head_issue_comment(self) -> None:
        class CommentClient(GitHubClient):
            def _request_all_pages(self, path: str) -> list[dict[str, object]]:
                if path.endswith("/reviews"):
                    return []
                return [
                    {
                        "user": {"login": "sahilsk11"},
                        "body": "@codex review\n\nHead SHA: current-sha",
                        "created_at": "2026-05-15T00:01:30Z",
                    },
                    {
                        "user": {"login": "codex"},
                        "body": "Codex review: didn't find any major issues.",
                        "created_at": "2026-05-15T00:02:00Z",
                    },
                ]

        client = CommentClient(token="token")

        decision = client.get_codex_review_decision(
            PullRequestRef("example", "repo", 42, "https://github.com/example/repo/pull/42"),
            "current-sha",
        )

        self.assertTrue(decision.approved)

    def test_codex_review_decision_accepts_issue_comment_after_unbound_manual_request_without_head(
        self,
    ) -> None:
        class CommentClient(GitHubClient):
            def _request_all_pages(self, path: str) -> list[dict[str, object]]:
                if path.endswith("/reviews"):
                    return []
                return [
                    {
                        "user": {"login": "sahilsk11"},
                        "body": "@codex review",
                        "created_at": "2026-05-15T00:01:30Z",
                    },
                    {
                        "user": {"login": "chatgpt-codex-connector"},
                        "body": "Codex Review: Didn't find any major issues. Keep it up!",
                        "created_at": "2026-05-15T00:02:00Z",
                    },
                ]

        client = CommentClient(token="token")

        decision = client.get_codex_review_decision(
            PullRequestRef("example", "repo", 42, "https://github.com/example/repo/pull/42")
        )

        self.assertTrue(decision.approved)

    def test_codex_review_decision_ignores_unbound_issue_comment_for_head(self) -> None:
        class CommentClient(GitHubClient):
            def _request_all_pages(self, path: str) -> list[dict[str, object]]:
                if path.endswith("/reviews"):
                    return []
                return [
                    {
                        "user": {"login": "sahilsk11"},
                        "body": "@codex review",
                        "created_at": "2026-05-15T00:01:30Z",
                    },
                    {
                        "user": {"login": "codex"},
                        "body": "Codex review: didn't find any major issues.",
                        "created_at": "2026-05-15T00:02:00Z",
                    }
                ]

        client = CommentClient(token="token")

        decision = client.get_codex_review_decision(
            PullRequestRef("example", "repo", 42, "https://github.com/example/repo/pull/42"),
            "current-sha",
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.summary, "No Codex review comments found for the current head.")

    def test_codex_author_requires_trusted_login(self) -> None:
        self.assertTrue(_is_codex_authored({"user": {"login": "codex"}}))
        self.assertTrue(_is_codex_authored({"user": {"login": "chatgpt-codex-connector"}}))
        self.assertTrue(_is_codex_authored({"user": {"login": "chatgpt-codex-connector[bot]"}}))
        self.assertFalse(_is_codex_authored({"user": {"login": "alice-codex-fan"}}))


class CliTest(unittest.TestCase):
    def test_list_prints_comment_count_without_review_thread_details(self) -> None:
        class ListGitHubClient:
            def get_unresolved_review_threads(self, pr: PullRequestRef) -> list[ReviewThread]:
                return [
                    ReviewThread(
                        id="THREAD2",
                        author="codex",
                        body="<sub><sub>P2 Badge</sub></sub> Please remove this smoke test.",
                        url="https://github.com/example/repo/pull/1#discussion_r2",
                        path="src/example.py",
                        line=12,
                        created_at="2026-05-15T00:01:00Z",
                    )
                ]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            store.init()
            store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="opencode",
                model=None,
                harness=None,
            )
            output = io.StringIO()

            with patch("overwatch.cli.GitHubClient", ListGitHubClient), redirect_stdout(output):
                _print_watched_prs(store, include_inactive=False)

        self.assertEqual(len(output.getvalue().splitlines()), 2)
        self.assertIn("1        https://github.com/example/repo/pull/42", output.getvalue())
        self.assertNotIn("P2 Badge", output.getvalue())


class StoreTest(unittest.TestCase):
    def test_watch_pr_upserts_provider_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")

            store.watch_pr(pr, provider="opencode", model=None, harness=None)
            watched = store.watch_pr(
                pr,
                provider="codex",
                model="gpt-5.5",
                harness="full",
                merge_on_bot_approval=True,
            )

            self.assertEqual(watched.provider, "codex")
            self.assertEqual(watched.model, "gpt-5.5")
            self.assertEqual(watched.harness, "full")
            self.assertTrue(watched.merge_on_bot_approval)
            self.assertEqual(len(store.unresolved_prs()), 1)

    def test_watched_prs_includes_latest_ci_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            watched = store.watch_pr(pr, provider="opencode", model=None, harness=None)
            store.record_ci_status(
                watched.id,
                CiStatus(state="success", head_sha="abc123", summary="CI passed", details={}),
            )

            rows = store.watched_prs()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].latest_ci_state, "success")
            self.assertEqual(rows[0].latest_head_sha, "abc123")

    def test_watched_prs_hides_inactive_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            watched = store.watch_pr(pr, provider="opencode", model=None, harness=None)
            store.mark_inactive(watched.id, status="merged")

            self.assertEqual(store.watched_prs(), [])
            self.assertEqual(store.watched_prs(include_inactive=True)[0].status, "merged")

    def test_watched_prs_hides_legacy_resolved_rows_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            watched = store.watch_pr(pr, provider="opencode", model=None, harness=None)
            store.mark_resolved(watched.id)

            self.assertEqual(store.watched_prs(), [])
            self.assertEqual(store.watched_prs(include_inactive=True)[0].status, "resolved")


class ApiTest(unittest.TestCase):
    def test_health_and_watch_list_get_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            app = create_app(store=store, static_dir=Path(tmpdir) / "static")

            with TestClient(app) as client:
                health = client.get("/api/health")
                created = client.post(
                    "/api/prs",
                    json={
                        "url": "https://github.com/example/repo/pull/42",
                        "provider": "codex",
                        "model": "gpt-5.5",
                        "autofix": True,
                    },
                )
                listed = client.get("/api/prs")
                fetched = client.get(f"/api/prs/{created.json()['id']}")

            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json(), {"status": "ok"})
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["provider"], "codex")
            self.assertTrue(created.json()["autofix"])
            self.assertEqual(len(listed.json()), 1)
            self.assertEqual(fetched.json()["url"], "https://github.com/example/repo/pull/42")

    def test_pr_buckets_split_active_done_and_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            active = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="opencode",
                model=None,
                harness=None,
            )
            done = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/43"),
                provider="opencode",
                model=None,
                harness=None,
            )
            store.mark_inactive(done.id, status="merged")
            app = create_app(store=store, static_dir=Path(tmpdir) / "static")

            with TestClient(app) as client:
                active_rows = client.get("/api/prs?bucket=active").json()
                done_rows = client.get("/api/prs?bucket=done").json()
                all_rows = client.get("/api/prs?bucket=all").json()

            self.assertEqual([row["id"] for row in active_rows], [active.id])
            self.assertEqual([row["id"] for row in done_rows], [done.id])
            self.assertEqual({row["id"] for row in all_rows}, {active.id, done.id})

    def test_events_include_ci_history_and_resolution_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="opencode",
                model="gpt-5.5",
                harness="full",
            )
            store.record_ci_status(
                watched.id,
                CiStatus(
                    state="failure",
                    head_sha="abc123",
                    summary="tests failed",
                    details={"jobs": ["test"]},
                ),
            )
            attempt_id = store.start_attempt(watched, "abc123")
            store.finish_attempt(attempt_id, status="failed", error="boom")
            app = create_app(store=store, static_dir=Path(tmpdir) / "static")

            with TestClient(app) as client:
                response = client.get(f"/api/prs/{watched.id}/events")

            self.assertEqual(response.status_code, 200)
            events = response.json()
            self.assertEqual(events["ci_history"][0]["state"], "failure")
            self.assertEqual(events["ci_history"][0]["details"], {"jobs": ["test"]})
            self.assertEqual(events["resolution_attempts"][0]["status"], "failed")
            self.assertEqual(events["resolution_attempts"][0]["error"], "boom")

    def test_refresh_records_single_github_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="opencode",
                model=None,
                harness=None,
            )
            github = FakeGitHubClient(
                CiStatus(
                    state="success",
                    head_sha="abc123",
                    summary="tests passed",
                    details={"source": "test"},
                    merged=True,
                )
            )
            app = create_app(
                store=store,
                github_factory=lambda: github,
                static_dir=Path(tmpdir) / "static",
            )

            with TestClient(app) as client:
                response = client.post(f"/api/prs/{watched.id}/refresh")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ci_status"]["head_sha"], "abc123")
            self.assertEqual(response.json()["pr"]["status"], "merged")
            self.assertEqual(store.watched_prs(), [])
            self.assertEqual(len(github.checked), 1)

    def test_static_assets_use_spa_fallback_and_reject_missing_api_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            static_dir = Path(tmpdir) / "static"
            static_dir.mkdir()
            (static_dir / "index.html").write_text("<main>app</main>", encoding="utf-8")
            (static_dir / "app.js").write_text("console.log('ok')", encoding="utf-8")
            app = create_app(store=Store(Path(tmpdir) / "overwatch.sqlite3"), static_dir=static_dir)

            with TestClient(app) as client:
                asset = client.get("/app.js")
                fallback = client.get("/dashboard/prs")
                api_missing = client.get("/api/missing")

            self.assertEqual(asset.status_code, 200)
            self.assertIn("console.log('ok')", asset.text)
            self.assertEqual(fallback.status_code, 200)
            self.assertIn("<main>app</main>", fallback.text)
            self.assertEqual(api_missing.status_code, 404)

    def test_app_uses_configured_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "configured.sqlite3"
            with patch.dict("os.environ", {"OVERWATCH_DB": str(db_path)}):
                app = create_app(static_dir=Path(tmpdir) / "static")

            with TestClient(app) as client:
                response = client.post(
                    "/api/prs",
                    json={"url": "https://github.com/example/repo/pull/42"},
                )

            self.assertEqual(response.status_code, 201)
            self.assertTrue(db_path.exists())


class WorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_success_does_not_remove_open_pr_from_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            store.watch_pr(pr, provider="opencode", model=None, harness=None)
            github = FakeGitHubClient(
                CiStatus(state="success", head_sha="abc123", summary="tests passed", details={})
            )

            await run_once(store, github=github, registry=ProviderRegistry([FakeProvider()]))

            rows = store.watched_prs()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "unresolved")
            self.assertEqual(rows[0].latest_ci_state, "success")
            self.assertEqual(len(github.reviewed), 0)
            self.assertEqual(len(github.merged), 0)

    async def test_success_merges_when_bot_approval_option_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            store.watch_pr(
                pr,
                provider="opencode",
                model=None,
                harness=None,
                merge_on_bot_approval=True,
            )
            github = FakeGitHubClient(
                CiStatus(state="success", head_sha="abc123", summary="tests passed", details={}),
                CodexReviewDecision(approved=True, summary="Codex review: no major issues"),
            )

            await run_once(store, github=github, registry=ProviderRegistry([FakeProvider()]))

            self.assertEqual(len(github.reviewed), 1)
            self.assertEqual(github.review_head_shas, ["abc123"])
            self.assertEqual(len(github.merged), 1)
            self.assertEqual(github.merge_head_shas, ["abc123"])
            self.assertEqual(store.watched_prs(), [])
            self.assertEqual(store.watched_prs(include_inactive=True)[0].status, "merged")

    async def test_no_ci_checks_merges_when_bot_approval_option_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            store.watch_pr(
                pr,
                provider="opencode",
                model=None,
                harness=None,
                merge_on_bot_approval=True,
            )
            github = FakeGitHubClient(
                CiStatus(
                    state="unknown",
                    head_sha="abc123",
                    summary="No CI checks reported.",
                    details={
                        "combined_status": {"statuses": []},
                        "check_runs": {"check_runs": []},
                        "actions": {"workflow_runs": []},
                    },
                ),
                CodexReviewDecision(approved=True, summary="Codex review: no major issues"),
            )

            await run_once(store, github=github, registry=ProviderRegistry([FakeProvider()]))

            self.assertEqual(len(github.reviewed), 1)
            self.assertEqual(len(github.merged), 1)
            self.assertEqual(github.merge_head_shas, ["abc123"])
            self.assertEqual(store.watched_prs(include_inactive=True)[0].status, "merged")

    async def test_unknown_ci_with_missing_check_details_does_not_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            store.watch_pr(
                pr,
                provider="opencode",
                model=None,
                harness=None,
                merge_on_bot_approval=True,
            )
            github = FakeGitHubClient(
                CiStatus(
                    state="unknown",
                    head_sha="abc123",
                    summary="No CI checks reported.",
                    details={
                        "combined_status": {"statuses": []},
                        "check_runs": {"error": "GitHub API error 403"},
                        "actions": {"workflow_runs": []},
                    },
                ),
                CodexReviewDecision(approved=True, summary="Codex review: no major issues"),
            )

            await run_once(store, github=github, registry=ProviderRegistry([FakeProvider()]))

            self.assertEqual(len(github.reviewed), 0)
            self.assertEqual(len(github.merged), 0)
            self.assertEqual(store.watched_prs(include_inactive=True)[0].status, "unresolved")

    async def test_success_does_not_merge_without_bot_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            store.watch_pr(
                pr,
                provider="opencode",
                model=None,
                harness=None,
                merge_on_bot_approval=True,
            )
            github = FakeGitHubClient(
                CiStatus(state="success", head_sha="abc123", summary="tests passed", details={}),
                CodexReviewDecision(approved=False, summary="Codex review: please fix this"),
            )

            await run_once(store, github=github, registry=ProviderRegistry([FakeProvider()]))

            self.assertEqual(len(github.reviewed), 1)
            self.assertEqual(len(github.merged), 0)
            self.assertEqual(store.watched_prs()[0].status, "unresolved")

    async def test_failing_ci_does_not_merge_even_with_bot_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            store.watch_pr(
                pr,
                provider="opencode",
                model=None,
                harness=None,
                merge_on_bot_approval=True,
            )
            github = FakeGitHubClient(
                CiStatus(state="failure", head_sha="abc123", summary="tests failed", details={}),
                CodexReviewDecision(approved=True, summary="Codex review: no major issues"),
            )

            await run_once(store, github=github, registry=ProviderRegistry([FakeProvider()]))

            self.assertEqual(len(github.reviewed), 0)
            self.assertEqual(len(github.merged), 0)
            self.assertEqual(store.watched_prs()[0].status, "unresolved")

    async def test_review_thread_failure_defers_merge_on_bot_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            store.watch_pr(
                pr,
                provider="opencode",
                model=None,
                harness=None,
                merge_on_bot_approval=True,
            )
            github = FakeGitHubClient(
                CiStatus(state="success", head_sha="abc123", summary="tests passed", details={}),
                CodexReviewDecision(approved=True, summary="Codex review: no major issues"),
                review_threads_error=RuntimeError("GraphQL unavailable"),
            )

            await run_once(store, github=github, registry=ProviderRegistry([FakeProvider()]))

            self.assertEqual(len(github.reviewed), 0)
            self.assertEqual(len(github.merged), 0)
            self.assertEqual(store.watched_prs()[0].status, "unresolved")

    async def test_review_thread_failure_still_allows_ci_autofix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            store.watch_pr(
                pr,
                provider="opencode",
                model=None,
                harness=None,
                autofix=True,
                merge_on_bot_approval=True,
            )
            github = FakeGitHubClient(
                CiStatus(state="failure", head_sha="abc123", summary="tests failed", details={}),
                review_threads_error=RuntimeError("GraphQL unavailable"),
            )
            provider = FakeProvider()

            await run_once(store, github=github, registry=ProviderRegistry([provider]))

            self.assertEqual(len(provider.calls), 1)
            prompt, _config = provider.calls[0]
            self.assertIn("CI failure:", prompt)
            self.assertIn("tests failed", prompt)

    async def test_autofix_triggers_provider_for_unresolved_review_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            store.watch_pr(
                pr,
                provider="opencode",
                model=None,
                harness=None,
                autofix=True,
                merge_on_bot_approval=True,
            )
            github = FakeGitHubClient(
                CiStatus(state="success", head_sha="abc123", summary="tests passed", details={}),
                review_threads=[
                    ReviewThread(
                        id="THREAD2",
                        author="codex",
                        body="Please remove this smoke test.",
                        url="https://github.com/example/repo/pull/42#discussion_r2",
                        path="src/example.py",
                        line=12,
                        created_at="2026-05-15T00:01:00Z",
                    )
                ],
            )
            provider = FakeProvider()

            await run_once(store, github=github, registry=ProviderRegistry([provider]))

            self.assertEqual(len(provider.calls), 1)
            prompt, _config = provider.calls[0]
            self.assertIn("Please remove this smoke test.", prompt)
            self.assertIn("reply on the thread", prompt)
            self.assertIn("Resolve the review thread", prompt)
            self.assertEqual(len(github.review_requests), 1)

    async def test_review_comments_do_not_autofix_without_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            store.watch_pr(pr, provider="opencode", model=None, harness=None)
            github = FakeGitHubClient(
                CiStatus(state="success", head_sha="abc123", summary="tests passed", details={}),
                review_threads=[
                    ReviewThread(
                        id="THREAD2",
                        author="codex",
                        body="Please remove this smoke test.",
                        url="https://github.com/example/repo/pull/42#discussion_r2",
                        path="src/example.py",
                        line=12,
                        created_at="2026-05-15T00:01:00Z",
                    )
                ],
            )
            provider = FakeProvider()

            await run_once(store, github=github, registry=ProviderRegistry([provider]))

            self.assertEqual(len(provider.calls), 0)
            self.assertEqual(len(github.review_requests), 0)

    async def test_merged_pr_is_hidden_from_default_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            store.watch_pr(pr, provider="opencode", model=None, harness=None)
            github = FakeGitHubClient(
                CiStatus(
                    state="success",
                    head_sha="abc123",
                    summary="tests passed",
                    details={},
                    merged=True,
                )
            )

            await run_once(store, github=github, registry=ProviderRegistry([FakeProvider()]))

            self.assertEqual(store.watched_prs(), [])
            self.assertEqual(store.watched_prs(include_inactive=True)[0].status, "merged")

    async def test_failure_triggers_provider_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            store.watch_pr(
                pr,
                provider="opencode",
                model="gpt-5.5",
                harness="full",
                autofix=True,
            )
            github = FakeGitHubClient(
                CiStatus(state="failure", head_sha="abc123", summary="tests failed", details={})
            )
            provider = FakeProvider()

            await run_once(store, github=github, registry=ProviderRegistry([provider]))

            self.assertEqual(len(github.checked), 1)
            self.assertEqual(len(provider.calls), 1)
            prompt, config = provider.calls[0]
            self.assertIn("https://github.com/example/repo/pull/42", prompt)
            self.assertEqual(config.model, "gpt-5.5")
            self.assertEqual(config.harness, "full")

    async def test_failure_attempts_are_bounded_per_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")
            watched = store.watch_pr(pr, provider="opencode", model=None, harness=None)
            store.start_attempt(watched, "abc123")
            github = FakeGitHubClient(
                CiStatus(state="failure", head_sha="abc123", summary="tests failed", details={})
            )
            provider = FakeProvider()

            await run_once(
                store,
                github=github,
                registry=ProviderRegistry([provider]),
                max_attempts_per_sha=1,
            )

            self.assertEqual(len(provider.calls), 0)


if __name__ == "__main__":
    unittest.main()
