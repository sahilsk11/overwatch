from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from overwatch.github import (
    CiStatus,
    PullRequestRef,
    _rollup_state,
    _summarize_status,
    parse_pr_url,
)
from overwatch.providers import AgentConfig, ProviderRegistry
from overwatch.store import Store
from overwatch.worker import run_once


class FakeGitHubClient:
    def __init__(self, status: CiStatus) -> None:
        self.status = status
        self.checked: list[PullRequestRef] = []

    def get_ci_status(self, pr: PullRequestRef) -> CiStatus:
        self.checked.append(pr)
        return self.status


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


class StoreTest(unittest.TestCase):
    def test_watch_pr_upserts_provider_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            pr = parse_pr_url("https://github.com/example/repo/pull/42")

            store.watch_pr(pr, provider="opencode", model=None, harness=None)
            watched = store.watch_pr(pr, provider="codex", model="gpt-5.5", harness="full")

            self.assertEqual(watched.provider, "codex")
            self.assertEqual(watched.model, "gpt-5.5")
            self.assertEqual(watched.harness, "full")
            self.assertEqual(len(store.unresolved_prs()), 1)


class WorkerTest(unittest.IsolatedAsyncioTestCase):
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
