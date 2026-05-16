# Overwatch Rearchitecture Plan

## Purpose

Overwatch is meant to be a reliable PR supervisor: observe a pull request, classify the real blockers, ask an agent to fix valid blockers, request another Codex review when appropriate, and merge only when the configured policy says it is safe.

The current implementation has proven too easy to perturb with tactical fixes. In PR #7, Codex review comments repeatedly found issues in the merge policy and review-request flow, and the spawned repair agent accepted at least one comment too literally because it did not have enough product context. This document lays out a redesign plan before more patches are added.

## Current Shape

The backend is small, but several important responsibilities are fused together:

- `src/overwatch/github.py` combines low-level GitHub REST/GraphQL transport, response parsing, CI rollup policy, Codex approval detection, review-thread discovery, review requests, and merge calls.
- `src/overwatch/worker.py` combines polling, policy decisions, attempt throttling, prompt construction, provider invocation, re-review requests, and merge decisions.
- `src/overwatch/store.py` stores watched PRs, CI history, and attempts, but does not model a review/fix/merge cycle as a first-class object.
- `src/overwatch/providers.py` shells out to agents, but has no explicit automation contract for safe non-interactive execution, session reuse, worktree ownership, or token/run limits.
- `tests/test_overwatch.py` is broad but centralized, making it easy to add case-specific tests without clarifying the underlying domain boundaries.

This is not one giant file, but it is still not layered in the way this product needs. The code has modules, not a stable domain model.

## Failure Modes Observed

1. No-CI merge policy was underspecified.

   Desired behavior: if there are no configured checks, then bot approval is enough; if any real check is pending or failing, it is not enough.

   The first fix used a summary string, `"No CI checks reported."`, as policy evidence. Codex correctly called that weak. The spawned fix then overcorrected by treating a check-runs API visibility error as a merge blocker, which conflicts with the intended private-repo/no-CI behavior.

2. Review requests used stale head SHAs.

   After an autofix provider pushed a new commit, Overwatch requested another Codex review using the old head SHA captured before the fix attempt. Any review/merge decision needs to be anchored to the refreshed PR head after the agent exits.

3. One-shot runs were mistaken for continuous watching.

   `--run-once` handled one batch of review comments, spawned Codex, and exited. Later Codex comments were not handled because no worker process remained active. This is expected from the code, but the operational model is unclear.

4. Spawned agents lacked product context.

   The repair prompt told the agent to address unresolved review comments, but did not include the core policy: "no configured CI means merge-eligible; real pending/failing CI blocks." The agent treated Codex review comments as instructions instead of hypotheses to evaluate against the product contract.

5. There is no turn budget.

   Attempts are capped per SHA, but not by a full review/fix/review cycle. A PR can move to a new SHA repeatedly, and Overwatch has no explicit "stop after N turns" safety rail.

6. Provider execution is implicit.

   The first local run left a `running` attempt because `codex exec` was not invoked with an explicit non-interactive automation mode. The provider contract needs to say what an agent command must do and how Overwatch records process exits.

7. Session context is not modeled.

   Overwatch currently creates fresh agent sessions. That keeps runs isolated, but loses the intent discussed in the originating Codex session. Reusing a session may help context, but it transfers ownership of that session to Overwatch and needs explicit user-facing semantics.

## Design Principles

1. Separate facts from policy.

   GitHub adapters should return normalized facts: head SHA, PR state, check contexts, review threads, review submissions, issue comments, and mergeability. They should not decide whether a PR is merge-safe.

2. Make policy explicit and testable.

   A merge policy service should answer questions such as:

   - Is CI passing?
   - Are there no configured checks?
   - Is CI unknown because GitHub data is incomplete?
   - Is there a head-scoped Codex approval?
   - Is this PR eligible for merge right now?

3. Treat review comments as hypotheses.

   Agent prompts should require validation against project policy and code evidence. "P1" from Codex is important signal, not an automatic instruction.

4. Model a review cycle as durable state.

   A "turn" should include observed head, blockers, agent attempt, pushed head, requested review, review result, and final decision. This creates inspectability and a hard limit.

5. Refresh after side effects.

   After an agent completes, Overwatch must re-fetch PR head and status before requesting review, counting turns, or merging.

6. Favor safe automation over silent loops.

   Long-running workers should expose current activity, attempt status, turn count, last prompt summary, and next scheduled action.

## Target Architecture

### Domain Layer

Add domain models that are independent of GitHub response shapes:

- `PullRequestSnapshot`
  - PR ref, state, draft flag, mergeability, head SHA, base branch.
- `CiSnapshot`
  - check contexts, workflow contexts, legacy statuses, source errors, and a derived enum.
- `ReviewSnapshot`
  - unresolved threads, Codex review requests, Codex approvals, review comments, and head binding.
- `WatchPolicy`
  - autofix enabled, merge on bot approval, turn budget, provider config, session strategy.
- `WatchTurn`
  - turn number, starting head, blockers, attempt status, ending head, review request, decision.

These should live outside `github.py`; the GitHub client maps raw API data into these objects.

### GitHub Integration Layer

Split `github.py` into narrower collaborators:

- `GitHubTransport`: authenticated REST/GraphQL request helper.
- `PullRequestGateway`: fetch PR metadata, merge PR, create issue comments.
- `CiGateway`: fetch check rollup/status/action data and preserve source errors.
- `ReviewGateway`: fetch review threads, review submissions, and issue comments.
- `CodexReviewParser`: identify trusted Codex authors and parse approval/error/actionable review bodies.

Important: the gateway returns facts, including API errors. It does not decide that no checks means safe.

### Policy Layer

Introduce pure services:

- `CiPolicy.classify(snapshot) -> CiDecision`
  - `passing`
  - `failing`
  - `pending`
  - `no_checks`
  - `incomplete`
- `CodexApprovalPolicy.evaluate(review_snapshot, head_sha) -> ApprovalDecision`
  - Formal GitHub `APPROVED` reviews must match head.
  - Issue-comment approvals must follow a review request that is scoped to the current head.
  - Operational Codex comments are ignored.
- `MergePolicy.evaluate(pr, ci, reviews, watch_policy) -> MergeDecision`
  - `passing` and approved: merge.
  - `no_checks` and approved: merge.
  - `failing`, `pending`, or `incomplete`: do not merge.
  - unresolved review threads: do not merge; maybe autofix.

The "no CI means assume pass" rule belongs here and should be expressed directly.

### Orchestration Layer

Replace the current `run_once` decision tree with a use case:

`ProcessWatchedPullRequest`

Steps:

1. Load watched PR and policy.
2. Fetch a fresh `PullRequestSnapshot`, `CiSnapshot`, and `ReviewSnapshot`.
3. Record the observation.
4. Build a `MergeDecision`.
5. If closed/merged, mark inactive.
6. If unresolved blockers and turn budget remains, create a `WatchTurn` and run provider.
7. After provider exits, refresh the PR snapshot.
8. If head changed or code changed, request Codex review for the refreshed head.
9. End the turn with explicit status.
10. If no blockers and merge decision permits, merge with the refreshed head SHA.

This use case should be the only place that mutates GitHub or the store.

### Agent Context Layer

Add an `AgentContext` concept that can be rendered into prompts:

- Product contract for this watch.
- PR description and current PR body.
- Current blockers.
- Relevant prior turns.
- The merge policy in plain English.
- Instructions to treat review comments as hypotheses.
- Explicit stop conditions and output expectations.

Initial implementation can use a summarized context string stored per watched PR. Later, this can support richer session handoff.

### Session Strategy

Support three modes:

- `fresh`:
  - Current behavior. Spawn a new agent session each turn.
  - Lowest risk of session contamination, weakest context.
- `context-summary`:
  - Spawn fresh sessions, but prepend a durable context summary maintained by Overwatch.
  - Recommended default.
- `attached-session`:
  - Use or resume a named Codex session.
  - The session becomes owned by Overwatch until the watch completes or is stopped.
  - Requires explicit user confirmation and UI/CLI warnings.

Do not make attached sessions the default. It is powerful but operationally surprising.

### Turn Budget

Add `--turns`, default `3`, maximum `10`.

Store turn count per watched PR, not only attempts per SHA. A turn is consumed when Overwatch launches a provider for a PR. Stop when the budget is exhausted and surface `needs-human` status.

The UI and CLI should show:

- turns used / max turns
- last attempt status
- last head SHA observed
- last provider command
- last error
- whether the worker is active

### Provider Contract

Make provider commands explicit:

- Default Codex command should be suitable for automation, or Overwatch should refuse to run Codex without an automation profile.
- Provider config should include working directory/worktree policy.
- Provider run output should be captured enough for diagnostics, with truncation.
- A provider attempt should never remain `running` if the parent process exits normally.

Recommended Codex automation profile:

```sh
codex exec --dangerously-bypass-approvals-and-sandbox --sandbox danger-full-access --color never
```

This should not be hidden. It should be documented and surfaced in `overwatch list` or PR events.

### Worktree Ownership

Overwatch should not rely on the caller's current checkout for long-running work.

Options:

- require `--worktree PATH`
- create `~/wt/<repo>-pr<number>-overwatch`
- verify the worktree is on the PR branch
- refuse to run if dirty unless `--allow-dirty` is set

For PR branches, Overwatch must fetch the latest head before each turn.

## Proposed Implementation Phases

### Phase 1: Stabilize Current Behavior

Goal: make existing behavior correct before broad refactors.

- Add `--turns`, default `3`, max `10`.
- Refresh PR head after provider completion before requesting Codex review.
- Persist a `watch_turns` table.
- Make Codex provider command automation explicit and documented.
- Add tests for:
  - stale head review request after provider push
  - turn budget exhaustion
  - run-once versus worker behavior
  - provider command failure being recorded as failed, not left running

Acceptance criteria:

- A one-shot run handles exactly one turn.
- A worker run handles later review comments until turn budget is exhausted.
- Review requests always include the refreshed head SHA.

### Phase 2: Extract Policy Services

Goal: stop encoding policy in `worker.py` and `github.py` helper conditionals.

- Create `CiPolicy`, `CodexApprovalPolicy`, and `MergePolicy`.
- Move approval body parsing into `CodexReviewParser`.
- Add table-driven tests for CI cases:
  - checks success
  - checks failure
  - checks pending
  - no checks
  - REST check-runs 403 but PR rollup shows no contexts
  - API data incomplete with no reliable no-checks signal
- Add table-driven tests for approval cases:
  - formal head-matched approval
  - formal stale approval
  - issue-comment approval after head-scoped request
  - issue-comment approval after stale request
  - operational Codex comment

Acceptance criteria:

- The intended no-CI rule is expressed in one pure policy test.
- GitHub API quirks are normalized before merge policy evaluation.

### Phase 3: Normalize GitHub Snapshots

Goal: GitHub data collection returns facts, not decisions.

- Split `GitHubClient` into gateways.
- Add `PullRequestSnapshot`, `CiSnapshot`, and `ReviewSnapshot`.
- Prefer PR-level check rollup when available.
- Preserve source errors separately from check contexts.
- Keep raw snippets in `details_json` only for diagnostics.

Acceptance criteria:

- `worker.py` no longer reads raw `details` dictionaries.
- CI summary text is never used as policy evidence.

### Phase 4: Durable Context and Better Prompts

Goal: spawned agents understand the product contract.

- Add `context_summary` to watched PRs.
- Add CLI/API fields for context.
- Include policy, prior decisions, and non-goals in provider prompts.
- Update prompt to require:
  - validate each review comment
  - explain invalid comments instead of changing code
  - preserve no-CI merge policy
  - refresh/push/request review expectations

Acceptance criteria:

- A spawned agent has enough context to reject a review comment that conflicts with the configured policy.
- Prompt snapshots are testable without invoking a real agent.

### Phase 5: Session Strategy

Goal: support context-rich operation without making session ownership surprising.

- Add `--session-strategy fresh|context-summary|attached-session`.
- Implement `context-summary` first.
- Design `attached-session` as an explicit, advanced mode with warnings.
- Store session IDs when supported by provider.

Acceptance criteria:

- Default mode remains predictable.
- Users can opt into richer context.
- The UI/CLI clearly states when Overwatch owns a session.

### Phase 6: Operational Visibility

Goal: make silent token burn unlikely.

- Show active worker status.
- Show in-progress attempts and elapsed time.
- Show turn counts.
- Show last provider command and last error.
- Add stop/pause controls in CLI/API.
- Add logs per turn.

Acceptance criteria:

- A user can tell whether Overwatch is actively spending tokens.
- A user can stop a watch before another turn starts.

## Open Questions

1. Should "no CI configured" be determined only from PR-level `statusCheckRollup`, or from a combination of rollup, actions list, and legacy statuses?
2. Should Overwatch auto-resolve review threads, or only reply and let GitHub/Codex mark them resolved?
3. Should merge-on-bot-approval require a Codex review after the last Overwatch-authored commit, or after any last commit?
4. How should attached-session mode address user interaction if the user keeps typing in the session?
5. Should the default provider be `codex` instead of `opencode` for this project's intended workflow?

## Immediate Recommendation

Do not keep patching PR #7 as the foundation for long-running behavior. Use it as evidence for the redesign.

The next PR should be Phase 1 only: turn budget, refreshed head after provider completion, explicit Codex automation command handling, and better attempt recording. That gives Overwatch a safer operational loop before the deeper policy extraction work begins.
