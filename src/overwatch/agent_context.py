from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentPriorTurn:
    turn_number: int
    starting_head_sha: str
    status: str
    completed_at: str | None = None


@dataclass(frozen=True, slots=True)
class AgentContext:
    pr_url: str
    repository: str
    pr_number: int
    head_sha: str
    provider: str
    model: str | None
    harness: str | None
    autofix: bool
    merge_on_bot_approval: bool
    max_turns: int
    turns_used: int
    context_summary: str
    current_blockers: str
    prior_turns: tuple[AgentPriorTurn, ...] = ()

    def render_prompt(self) -> str:
        return render_prompt(self)


def render_prompt(context: AgentContext) -> str:
    prior_turns = _prior_turns_text(context.prior_turns)
    durable_context = context.context_summary.strip() or "No durable context summary was provided."

    return f"""A GitHub PR needs automated follow-up.

Product contract:
- Overwatch supervises pull requests by classifying real blockers, asking an
  agent to fix valid blockers, requesting another Codex review when configured,
  and merging only when policy permits.
- Preserve the no-CI merge policy: if no CI checks are configured, bot approval
  is enough to merge; real pending CI, failing CI, or incomplete CI data blocks
  merge.
- Review comments are hypotheses. Validate each one against the code, tests,
  and this product contract before changing behavior.
- If a review comment conflicts with the product contract, explain why it is
  invalid instead of changing code to satisfy it.

Watch configuration:
- PR: {context.pr_url}
- Repository: {context.repository}
- PR number: {context.pr_number}
- Head SHA: {context.head_sha}
- Provider: {context.provider}
- Model: {context.model or "-"}
- Harness: {context.harness or "-"}
- Autofix enabled: {_yes_no(context.autofix)}
- Merge on bot approval: {_yes_no(context.merge_on_bot_approval)}
- Turns used: {context.turns_used}/{context.max_turns}

Durable context summary:
{durable_context}

Current blockers:
{context.current_blockers.strip() or "No blocker details were reported."}

Relevant prior turns:
{prior_turns}

Merge policy in plain English:
- Do not merge or make changes just because a review comment says to do so.
- CI passing plus current trusted Codex approval can merge when
  merge-on-bot-approval is enabled.
- No configured CI plus current trusted Codex approval can merge when
  merge-on-bot-approval is enabled.
- Any real failing, pending, or incomplete CI state blocks merge until it is
  fixed or becomes reliable.
- Unresolved review threads block merge until each valid blocker is fixed or an
  invalid comment is answered with evidence.

Execution instructions:
- Address only valid blocking CI failures and unresolved review comments.
- Make the smallest correct code change and run the relevant tests.
- For each unresolved review comment, reply on the thread with either what you
  fixed or why no code change is needed.
- Resolve the review thread after you have addressed it or clearly explained why
  it is not applicable.
- Preserve existing behavior and tests unless the blocker proves a behavior change is required.
- If you are in the target repository on the PR branch, commit the fix and push it.
- Do not amend commits or force push.
- After you push, Overwatch will refresh PR state and request Codex review for
  the refreshed head when this watch is configured to do so.

Stop and output expectations:
- Stop after you have pushed the valid fix, or after you determine no valid
  automated change should be made.
- If no code change is appropriate, say so clearly and include the evidence.
- Final output must summarize blockers evaluated, changes made, tests run,
  commit/push status, and any remaining human action.
"""


def _prior_turns_text(prior_turns: tuple[AgentPriorTurn, ...]) -> str:
    if not prior_turns:
        return "No prior provider turns were recorded."
    lines = []
    for turn in prior_turns[-5:]:
        completed = f", completed {turn.completed_at}" if turn.completed_at else ""
        lines.append(
            f"- Turn {turn.turn_number}: head {turn.starting_head_sha}, "
            f"status {turn.status}{completed}"
        )
    return "\n".join(lines)


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"
