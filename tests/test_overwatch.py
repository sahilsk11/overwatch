from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from overwatch.github import (
    CiStatus,
    CodexReviewDecision,
    PullRequestRef,
    _body_says_codex_approved,
    _rollup_state,
    _summarize_status,
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
    ) -> None:
        self.status = status
        self.decision = decision or CodexReviewDecision(approved=False, summary="No approval")
        self.checked: list[PullRequestRef] = []
        self.reviewed: list[PullRequestRef] = []
        self.merged: list[PullRequestRef] = []

    def get_ci_status(self, pr: PullRequestRef) -> CiStatus:
        self.checked.append(pr)
        return self.status

    def get_codex_review_decision(self, pr: PullRequestRef) -> CodexReviewDecision:
        self.reviewed.append(pr)
        return self.decision

    def merge_pr(self, pr: PullRequestRef) -> None:
        self.merged.append(pr)


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
            self.assertEqual(len(github.merged), 1)
            self.assertEqual(store.watched_prs(), [])
            self.assertEqual(store.watched_prs(include_inactive=True)[0].status, "merged")

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
            store.watch_pr(pr, provider="opencode", model="gpt-5.5", harness="full")
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
