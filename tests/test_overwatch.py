from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from overwatch.api import create_app
from overwatch.cli import main
from overwatch.domain import PullRequestSnapshot
from overwatch.github import PullRequestRef, parse_pr_url
from overwatch.providers import AgentConfig, ProviderRegistry
from overwatch.store import Store
from overwatch.worker import build_supervisor_prompt, run_tick


class FakeProvider:
    def __init__(self, provider_id: str = "codex") -> None:
        self.provider_id = provider_id
        self.calls: list[tuple[str, AgentConfig]] = []

    async def run(self, prompt: str, config: AgentConfig) -> None:
        self.calls.append((prompt, config))


class ExplodingProvider:
    provider_id = "codex"

    async def run(self, prompt: str, config: AgentConfig) -> None:
        raise FileNotFoundError("codex")


class FakeGitHub:
    def __init__(self, *, states: dict[str, tuple[str, bool]] | None = None) -> None:
        self.states = states or {}

    def get_pr_snapshot(self, pr: PullRequestRef) -> PullRequestSnapshot:
        state, merged = self.states.get(pr.url, ("open", False))
        return PullRequestSnapshot(
            ref=pr,
            state=state,
            draft=False,
            mergeable=True,
            head_sha="abc123",
            base_branch="main",
            merged=merged,
        )


class StoreTest(unittest.TestCase):
    def test_add_list_pause_resume_stop_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watch = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
                context_summary="Original chat context",
                session_strategy="attached-session",
                session_id="session-123",
            )

            self.assertEqual(watch.owner, "example")
            self.assertEqual(watch.repo, "repo")
            self.assertEqual(watch.number, 42)
            self.assertEqual(store.unresolved_prs()[0].context_summary, "Original chat context")

            store.pause_watch(watch.id)
            self.assertEqual(store.unresolved_prs(), [])
            self.assertEqual(store.watched_prs()[0].status, "paused")

            store.resume_watch(watch.id)
            self.assertEqual(store.unresolved_prs()[0].status, "unresolved")

            store.stop_watch(watch.id)
            self.assertEqual(store.watched_prs(), [])
            self.assertEqual(store.watched_prs(include_inactive=True)[0].status, "stopped")

    def test_parse_pr_url_rejects_non_pull_request_url(self) -> None:
        with self.assertRaises(ValueError):
            parse_pr_url("https://github.com/example/repo/issues/42")


class CliTest(unittest.TestCase):
    def test_cli_adds_and_lists_watch(self) -> None:
        class ListGitHubClient:
            def get_unresolved_review_threads(self, pr: object) -> list[object]:
                return []

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "overwatch.sqlite3"
            add_argv = [
                "overwatch",
                "--db",
                str(db_path),
                "--context",
                "Use the original chat.",
                "--max-turns",
                "5",
                "https://github.com/example/repo/pull/42",
            ]
            list_argv = ["overwatch", "--db", str(db_path), "list"]
            output = io.StringIO()

            with patch.object(sys, "argv", add_argv), redirect_stdout(output):
                main()
            with (
                patch.object(sys, "argv", list_argv),
                patch("overwatch.cli.GitHubClient", ListGitHubClient),
                redirect_stdout(output),
            ):
                main()
            watched = Store(db_path).watched_prs(include_inactive=True)[0]

        self.assertIn("watching https://github.com/example/repo/pull/42", output.getvalue())
        self.assertIn("https://github.com/example/repo/pull/42", output.getvalue())
        self.assertEqual(watched.max_turns, 5)


class ApiTest(unittest.TestCase):
    def test_api_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )
            app = create_app(store=store, static_dir=Path(tmpdir) / "static")

            with TestClient(app) as client:
                listed = client.get("/api/prs")
                fetched = client.get(f"/api/prs/{watched.id}")
                create_response = client.post(
                    "/api/prs",
                    json={"url": "https://github.com/example/repo/pull/43"},
                )
                refresh_response = client.post(f"/api/prs/{watched.id}/refresh")
                pause_response = client.post(f"/api/prs/{watched.id}/pause")

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(fetched.status_code, 200)
        self.assertIn(create_response.status_code, {404, 405})
        self.assertIn(refresh_response.status_code, {404, 405})
        self.assertIn(pause_response.status_code, {404, 405})


class WorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_tick_launches_one_supervisor_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
                session_strategy="attached-session",
                session_id="session-123",
                context_summary="Original chat context",
            )
            provider = FakeProvider()

            await run_tick(store, github=FakeGitHub(), registry=ProviderRegistry([provider]))
            refreshed = store.get_pr(watched.id)
            turns = store.watch_turns(watched.id)

        self.assertEqual(len(provider.calls), 1)
        prompt, config = provider.calls[0]
        self.assertEqual(config.provider, "codex")
        self.assertEqual(config.model, "gpt-5.5")
        self.assertIn("overwatch --db", prompt)
        self.assertIn("gh", prompt)
        self.assertIn("separate child Codex", prompt)
        self.assertIn("https://github.com/example/repo/pull/42", prompt)
        self.assertIn("Original chat context", prompt)
        self.assertIn("session-123", prompt)
        self.assertIn("autofix=no", prompt)
        self.assertIn("merge_on_bot_approval=no", prompt)
        self.assertIn("comment `@codex review`", prompt)
        self.assertIn("Do not post duplicate review requests", prompt)
        self.assertIn("Make one focused repair attempt, then stop.", prompt)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.turns_used, 1)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].starting_head_sha, "supervisor-tick")
        self.assertEqual(turns[0].status, "completed")

    async def test_run_tick_uses_codex_supervisor_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="opencode",
                model="gpt-5.5",
                harness=None,
            )
            provider = FakeProvider("codex")

            await run_tick(store, github=FakeGitHub(), registry=ProviderRegistry([provider]))

        self.assertEqual(len(provider.calls), 1)
        _, config = provider.calls[0]
        self.assertEqual(config.provider, "codex")
        self.assertEqual(config.model, "gpt-5.5")

    async def test_run_tick_marks_claimed_turns_failed_on_unexpected_provider_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )

            await run_tick(
                store,
                github=FakeGitHub(),
                registry=ProviderRegistry([ExplodingProvider()]),
            )
            turns = store.watch_turns(watched.id)

        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].status, "failed")

    async def test_run_tick_does_nothing_without_active_watches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            provider = FakeProvider()

            await run_tick(store, github=FakeGitHub(), registry=ProviderRegistry([provider]))

        self.assertEqual(provider.calls, [])

    async def test_run_tick_marks_exhausted_watches_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
                max_turns=1,
            )
            turn = store.start_supervisor_turn(watched)
            store.finish_supervisor_turn(turn.id, status="completed")
            provider = FakeProvider()

            await run_tick(store, github=FakeGitHub(), registry=ProviderRegistry([provider]))

            refreshed = store.get_pr(watched.id)
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertEqual(refreshed.status, "needs-human")
            self.assertEqual(refreshed.turns_used, 1)
            self.assertEqual(provider.calls, [])

    async def test_run_tick_marks_merged_and_closed_watches_inactive_before_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            merged = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/41"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )
            closed = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )
            open_watch = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/43"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )
            provider = FakeProvider()
            github = FakeGitHub(
                states={
                    merged.url: ("closed", True),
                    closed.url: ("closed", False),
                    open_watch.url: ("open", False),
                }
            )

            await run_tick(store, github=github, registry=ProviderRegistry([provider]))
            merged_refreshed = store.get_pr(merged.id)
            closed_refreshed = store.get_pr(closed.id)
            open_refreshed = store.get_pr(open_watch.id)

        self.assertIsNotNone(merged_refreshed)
        self.assertIsNotNone(closed_refreshed)
        self.assertIsNotNone(open_refreshed)
        assert merged_refreshed is not None
        assert closed_refreshed is not None
        assert open_refreshed is not None
        self.assertEqual(merged_refreshed.status, "merged")
        self.assertEqual(closed_refreshed.status, "closed")
        self.assertEqual(open_refreshed.status, "unresolved")
        self.assertEqual(open_refreshed.turns_used, 1)
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("https://github.com/example/repo/pull/43", provider.calls[0][0])
        self.assertNotIn("https://github.com/example/repo/pull/41", provider.calls[0][0])
        self.assertNotIn("https://github.com/example/repo/pull/42", provider.calls[0][0])

    def test_supervisor_prompt_is_cron_friendly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watch = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )

            prompt = build_supervisor_prompt(store, [watch])

        self.assertIn("This program is intentionally just a wake-up call.", prompt)
        self.assertIn("overwatch --db", prompt)
        self.assertIn("Use the GitHub CLI (`gh`)", prompt)


if __name__ == "__main__":
    unittest.main()
