# 2026-05-25 Overwatch Tick Claims Phased Implementation Plan

## Goal

Make Overwatch safe for scheduler-driven processing:

- `overwatch <PR>` remains enqueue-only.
- `overwatch --tick` can run from cron/systemd timer without duplicate processing.
- SQLite is the source of truth for queued/processing/completed state.
- Supervisor command/output/error logs are persisted and visible through the existing API/UI log path.
- SAS deployment can run ticks from systemd timer instead of a custom forever loop.

Research artifact: `/home/sahil/artifacts/overwatch-scheduled-tick-concurrency/eng-research-2026-05-25.md`.

## Phase 1: Store-Level Atomic Claims And Recovery

Status: DONE

Implement DB-owned processing state in `src/overwatch/store.py`.

- Add `processing` to active/control status handling where appropriate.
- Add supervisor log columns to `watch_turns`: `provider`, `model`, `provider_command`, `provider_output`, `error`.
- Add migration guards for the new `watch_turns` columns.
- Replace non-atomic supervisor claiming with a store method that:
  - atomically transitions one eligible `watched_prs` row from `unresolved` to `processing`;
  - increments `turns_used`;
  - creates a `watch_turns` row with `status = 'running'`;
  - skips rows at turn budget and marks them `needs-human`.
- Add methods to finish a supervisor turn that update the `watch_turns` row, persist command/output/error, and release the PR back to `unresolved` when appropriate.
- Add stale processing recovery for old running turns.
- Add focused store tests for duplicate claim prevention, finish behavior, and stale recovery.

Notes:

- `start_supervisor_turn()` remains as a compatibility wrapper around the new atomic claim path.
- `watched_prs.status = 'processing'` is now the durable ownership marker for active supervisor work.
- `watch_turns` now carries supervisor provider/log/error fields used by later API/UI work.
- Stale recovery currently marks old running turns failed with `stale processing recovered` and releases the PR to `unresolved`.

## Phase 2: Worker Integration And Supervisor Log Persistence

Status: DONE

Update `src/overwatch/worker.py` to use the store claim API.

- Remove local claim bookkeeping that can race.
- Call stale recovery before claiming.
- Persist successful `ProviderRunResult` command/output into the claimed turn.
- Persist `ProviderRunError` result and unexpected errors into the claimed turn.
- Preserve turn-budget behavior and existing prompt behavior.
- Add worker tests proving:
  - provider output appears in turn/event storage;
  - failed providers record error/logs;
  - an already-processing watch is not picked up by a second tick.

Notes:

- Worker calls stale recovery before claiming.
- Worker now claims via `Store.claim_supervisor_turn()`.
- `ProviderRunResult` and `ProviderRunError.result` are persisted onto `watch_turns`.
- Unexpected provider exceptions are recorded as turn errors.

## Phase 3: API/UI/CLI Visibility

Status: DONE

Expose the new turn/log state cleanly.

- Include `watch_turns` in `/api/prs/{id}/events` alongside CI history and attempts, or map supervisor turns into existing event/log output.
- Ensure `watched_prs.worker_status` / list output shows `processing` as running/processing.
- Ensure the frontend Logs panel includes supervisor turn command/output/error.
- Add API/frontend tests or focused API tests for event payload visibility.

Notes:

- `/api/prs/{id}/events` now returns `watch_turns` in addition to CI history and resolution attempts.
- API responses include supervisor turn command/output/error.
- Frontend event mapping and log collection now read supervisor turn payloads.
- Processing watches show `worker_status = running` through the existing summary/detail paths.

## Phase 4: SAS Timer Deployment

Status: DONE

In `/home/sahil/projects/sas`, prepare a separate worktree and PR that replaces the long-running Overwatch worker service with a systemd timer-driven tick.

- Add `overwatch_tick-<user>.service` template with `Type=oneshot` and `ExecStart=... overwatch --tick`.
- Add `overwatch_tick-<user>.timer` template with one-minute cadence.
- Update Ansible role cleanup to remove/disable old `overwatch_worker-*.service` units.
- Enable/start timers per Overwatch instance.
- Preserve dashboard service.
- Add or update docs to make the scheduler-owned model explicit.

Notes:

- SAS work was completed in `/home/sahil/wt/sas-overwatch-tick-timer`.
- SAS PR opened: `https://github.com/sahilsk11/sas/pull/100`.
- Dashboard service remains long-running.
- Former `overwatch_worker-*.service` cleanup is handled by the role.
- New `overwatch_tick-<user>.timer` invokes a oneshot `overwatch --tick` every minute.

## Final Verification

Status: DONE

- Run lint and tests in Overwatch.
- Run a local harness that creates a temp DB, registers watches, runs overlapping or repeated ticks with a fake provider, and verifies:
  - only one claim is active per PR;
  - processing state is observable;
  - logs are persisted into API-visible events;
  - finished work returns to the correct state.
- Run relevant SAS syntax/checks for the Ansible role if Phase 4 is implemented.

Result:

- `uv run ruff check src tests` passed.
- `uv run pytest -q` passed with 17 tests.
- `npm run typecheck` passed.
- Integrated harness passed:
  - created a temp DB and two watches;
  - manually held one watch in `processing`;
  - ran `run_tick()` with a fake provider;
  - verified the held watch was skipped;
  - verified the runnable watch was processed once;
  - verified supervisor command/output were visible via `/api/prs/{id}` and `/api/prs/{id}/events`.
- SAS Phase 4 validation: `bin/sas validate` passed in `/home/sahil/wt/sas-overwatch-tick-timer`.
