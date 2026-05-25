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
from overwatch.github import CiStatus, PullRequestRef, parse_pr_url
from overwatch.providers import AgentConfig, ProviderRegistry, ProviderRunError, ProviderRunResult
from overwatch.store import Store
from overwatch.worker import build_supervisor_prompt, run_tick


class FakeProvider:
    def __init__(
        self,
        provider_id: str = "codex",
        result: ProviderRunResult | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.result = result or ProviderRunResult(
            command="codex exec --model gpt-5.5",
            output="supervisor completed",
        )
        self.calls: list[tuple[str, AgentConfig]] = []

    async def run(self, prompt: str, config: AgentConfig) -> ProviderRunResult:
        self.calls.append((prompt, config))
        return self.result


class FailingProvider:
    provider_id = "codex"

    async def run(self, prompt: str, config: AgentConfig) -> ProviderRunResult:
        result = ProviderRunResult(
            command="codex exec --model gpt-5.5",
            output="stdout:\npartial supervisor log\n\nstderr:\nprovider failed",
        )
        raise ProviderRunError("codex exited with 1", result)


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


class FailingGitHub:
    def __init__(self, exception: Exception | None = None) -> None:
        self.exception = exception or OSError("network unavailable")

    def get_pr_snapshot(self, pr: PullRequestRef) -> PullRequestSnapshot:
        raise self.exception


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

    def test_supervisor_claim_prevents_duplicate_processing_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )

            claimed = store.claim_supervisor_turn(provider="codex", model="gpt-5.5")
            duplicate = store.claim_supervisor_turn(provider="codex", model="gpt-5.5")
            with self.assertRaises(RuntimeError):
                store.start_supervisor_turn(watched)

        self.assertIsNotNone(claimed)
        assert claimed is not None
        claimed_watch, claimed_turn = claimed
        self.assertEqual(claimed_watch.id, watched.id)
        self.assertEqual(claimed_watch.status, "processing")
        self.assertEqual(claimed_watch.turns_used, 1)
        self.assertEqual(claimed_turn.status, "running")
        self.assertEqual(claimed_turn.provider, "codex")
        self.assertEqual(claimed_turn.model, "gpt-5.5")
        self.assertIsNone(duplicate)

    def test_supervisor_finish_persists_logs_and_releases_processing_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )
            claimed = store.claim_supervisor_turn(provider="codex", model="gpt-5.5")
            assert claimed is not None
            _, turn = claimed

            store.finish_supervisor_turn(
                turn.id,
                status="failed",
                provider_command="codex exec",
                provider_output="supervisor output",
                error="provider failed",
            )
            refreshed = store.get_pr(watched.id)
            turns = store.watch_turns(watched.id)

        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "unresolved")
        self.assertEqual(turns[0].status, "failed")
        self.assertEqual(turns[0].provider_command, "codex exec")
        self.assertEqual(turns[0].provider_output, "supervisor output")
        self.assertEqual(turns[0].error, "provider failed")
        self.assertIsNotNone(turns[0].completed_at)

    def test_supervisor_claim_skips_exhausted_watches_and_marks_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            exhausted = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/41"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
                max_turns=1,
            )
            runnable = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )
            turn = store.start_supervisor_turn(exhausted)
            store.finish_supervisor_turn(turn.id, status="completed")

            claimed = store.claim_supervisor_turn(provider="codex", model="gpt-5.5")
            exhausted_refreshed = store.get_pr(exhausted.id)

        self.assertIsNotNone(claimed)
        assert claimed is not None
        claimed_watch, _ = claimed
        self.assertEqual(claimed_watch.id, runnable.id)
        self.assertIsNotNone(exhausted_refreshed)
        assert exhausted_refreshed is not None
        self.assertEqual(exhausted_refreshed.status, "needs-human")

    def test_recover_stale_processing_marks_turn_failed_and_releases_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )
            claimed = store.claim_supervisor_turn(provider="codex", model="gpt-5.5")
            assert claimed is not None
            _, turn = claimed
            old_timestamp = "2000-01-01T00:00:00+00:00"
            with store.connect() as conn:
                conn.execute(
                    "update watch_turns set created_at = ? where id = ?",
                    (old_timestamp, turn.id),
                )

            recovered = store.recover_stale_processing(older_than_seconds=1)
            refreshed = store.get_pr(watched.id)
            turns = store.watch_turns(watched.id)

        self.assertEqual(recovered, 1)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "unresolved")
        self.assertEqual(turns[0].status, "failed")
        self.assertEqual(turns[0].error, "stale processing recovered")

    def test_recover_stale_processing_ignores_resolution_attempt_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )
            store.start_attempt(watched, "head-sha")
            old_timestamp = "2000-01-01T00:00:00+00:00"
            with store.connect() as conn:
                conn.execute(
                    """
                    update watch_turns
                    set created_at = ?
                    where watched_pr_id = ?
                    """,
                    (old_timestamp, watched.id),
                )

            recovered = store.recover_stale_processing(older_than_seconds=1)
            refreshed = store.get_pr(watched.id)
            _, attempts, turns = store.pr_events(watched.id)

        self.assertEqual(recovered, 0)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "unresolved")
        self.assertEqual(attempts[0].status, "running")
        self.assertEqual(turns[0].status, "running")
        self.assertIsNone(turns[0].error)

    def test_watched_prs_reports_latest_diagnostics_by_event_recency(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
                max_turns=2,
            )
            attempt_id = store.start_attempt(watched, "head-sha")
            store.finish_attempt(
                attempt_id,
                status="failed",
                provider_command="codex old",
                provider_output="old attempt output",
                error="old attempt error",
            )
            with store.connect() as conn:
                conn.execute(
                    """
                    update resolution_attempts
                    set completed_at = '2000-01-01T00:00:00+00:00'
                    where id = ?
                    """,
                    (attempt_id,),
                )
                conn.execute(
                    """
                    update watch_turns
                    set completed_at = '2000-01-01T00:00:00+00:00'
                    where id = (
                        select watch_turn_id
                        from resolution_attempts
                        where id = ?
                    )
                    """,
                    (attempt_id,),
                )
            claimed = store.claim_supervisor_turn(provider="codex", model="gpt-5.5")
            assert claimed is not None
            _, turn = claimed
            store.finish_supervisor_turn(
                turn.id,
                status="failed",
                provider_command="codex new",
                provider_output="new supervisor output",
                error="new supervisor error",
            )
            with store.connect() as conn:
                conn.execute(
                    """
                    update watch_turns
                    set completed_at = '2001-01-01T00:00:00+00:00'
                    where id = ?
                    """,
                    (turn.id,),
                )

            listed = store.watched_prs(include_inactive=True)

        self.assertEqual(listed[0].last_attempt_status, "failed")
        self.assertEqual(listed[0].last_provider_command, "codex new")
        self.assertEqual(listed[0].last_provider_output, "new supervisor output")
        self.assertEqual(listed[0].last_error, "new supervisor error")


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

    def test_api_events_include_supervisor_turn_logs_and_processing_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )
            store.record_ci_status(
                watched.id,
                CiStatus(
                    state="failure",
                    head_sha="abc123456789",
                    summary="checks failed",
                    details={"workflow": "test"},
                ),
            )
            claimed = store.claim_supervisor_turn(provider="codex", model="gpt-5.5")
            assert claimed is not None
            _, turn = claimed
            app = create_app(store=store, static_dir=Path(tmpdir) / "static")

            with TestClient(app) as client:
                listed = client.get("/api/prs")
                detail = client.get(f"/api/prs/{watched.id}")

                store.finish_supervisor_turn(
                    turn.id,
                    status="failed",
                    provider_command="codex exec --model gpt-5.5",
                    provider_output="supervisor output",
                    error="provider failed",
                )
                events = client.get(f"/api/prs/{watched.id}/events")

        self.assertEqual(listed.status_code, 200)
        listed_payload = listed.json()
        self.assertEqual(listed_payload[0]["status"], "processing")
        self.assertEqual(listed_payload[0]["worker_status"], "running")
        self.assertEqual(listed_payload[0]["active_attempt_status"], "running")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["worker_status"], "running")
        self.assertEqual(events.status_code, 200)
        events_payload = events.json()
        self.assertEqual(events_payload["ci_history"][0]["summary"], "checks failed")
        self.assertEqual(events_payload["resolution_attempts"], [])
        self.assertEqual(len(events_payload["watch_turns"]), 1)
        turn_payload = events_payload["watch_turns"][0]
        self.assertEqual(turn_payload["status"], "failed")
        self.assertEqual(turn_payload["provider"], "codex")
        self.assertEqual(turn_payload["model"], "gpt-5.5")
        self.assertEqual(turn_payload["provider_command"], "codex exec --model gpt-5.5")
        self.assertEqual(turn_payload["provider_output"], "supervisor output")
        self.assertEqual(turn_payload["error"], "provider failed")


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
            listed = store.watched_prs(include_inactive=True)

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
        self.assertEqual(turns[0].provider_command, "codex exec --model gpt-5.5")
        self.assertEqual(turns[0].provider_output, "supervisor completed")
        self.assertEqual(listed[0].last_provider_command, "codex exec --model gpt-5.5")
        self.assertEqual(listed[0].last_provider_output, "supervisor completed")

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
        self.assertEqual(turns[0].error, "codex")

    async def test_run_tick_records_provider_failure_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )

            await run_tick(store, registry=ProviderRegistry([FailingProvider()]))
            turns = store.watch_turns(watched.id)
            listed = store.watched_prs(include_inactive=True)

        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].status, "failed")
        self.assertEqual(turns[0].provider_command, "codex exec --model gpt-5.5")
        self.assertIn("partial supervisor log", turns[0].provider_output or "")
        self.assertEqual(turns[0].error, "codex exited with 1")
        self.assertIn("partial supervisor log", listed[0].last_provider_output or "")
        self.assertEqual(listed[0].last_error, "codex exited with 1")

    async def test_run_tick_does_nothing_without_active_watches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            provider = FakeProvider()

            await run_tick(store, github=FakeGitHub(), registry=ProviderRegistry([provider]))

        self.assertEqual(provider.calls, [])

    async def test_run_tick_skips_already_processing_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            processing = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )
            runnable = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/43"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )
            claimed = store.claim_supervisor_turn(provider="codex", model="gpt-5.5")
            assert claimed is not None
            provider = FakeProvider()

            await run_tick(store, registry=ProviderRegistry([provider]))
            processing_refreshed = store.get_pr(processing.id)
            runnable_refreshed = store.get_pr(runnable.id)
            processing_turns = store.watch_turns(processing.id)
            runnable_turns = store.watch_turns(runnable.id)

        self.assertEqual(len(provider.calls), 1)
        prompt, _ = provider.calls[0]
        self.assertNotIn("https://github.com/example/repo/pull/42", prompt)
        self.assertIn("https://github.com/example/repo/pull/43", prompt)
        self.assertIsNotNone(processing_refreshed)
        assert processing_refreshed is not None
        self.assertEqual(processing_refreshed.status, "processing")
        self.assertEqual(processing_refreshed.turns_used, 1)
        self.assertIsNotNone(runnable_refreshed)
        assert runnable_refreshed is not None
        self.assertEqual(runnable_refreshed.status, "unresolved")
        self.assertEqual(runnable_refreshed.turns_used, 1)
        self.assertEqual(len(processing_turns), 1)
        self.assertEqual(processing_turns[0].status, "running")
        self.assertEqual(len(runnable_turns), 1)
        self.assertEqual(runnable_turns[0].status, "completed")

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

    async def test_run_tick_continues_when_pruning_snapshot_lookup_has_network_error(self) -> None:
        await self._assert_run_tick_continues_after_pruning_error(OSError("network unavailable"))

    async def test_run_tick_continues_when_pruning_snapshot_lookup_has_bad_response(self) -> None:
        await self._assert_run_tick_continues_after_pruning_error(ValueError("malformed JSON"))

    async def _assert_run_tick_continues_after_pruning_error(self, exception: Exception) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "overwatch.sqlite3")
            watched = store.watch_pr(
                parse_pr_url("https://github.com/example/repo/pull/42"),
                provider="codex",
                model="gpt-5.5",
                harness=None,
            )
            provider = FakeProvider()

            await run_tick(
                store,
                github=FailingGitHub(exception),
                registry=ProviderRegistry([provider]),
            )
            refreshed = store.get_pr(watched.id)

        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "unresolved")
        self.assertEqual(refreshed.turns_used, 1)
        self.assertEqual(len(provider.calls), 1)

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
