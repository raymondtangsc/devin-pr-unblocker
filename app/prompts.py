"""Prompt construction for Devin sessions.

The prompt is the real interface to the agent, so it is treated as code rather
than as a string literal buried in the orchestrator. Three principles shape it:

1.  **State the goal in terms of an observable end state**, not a list of steps.
    "``mergeable_state`` is no longer ``dirty``" is checkable; "rebase the branch"
    is not.
2.  **Name the failure modes explicitly.** The expensive mistake here is
    resolving a conflict by discarding the contributor's work — it produces a
    green build that silently deletes the feature. That gets its own paragraph.
3.  **Bound the blast radius.** No force-push to shared branches, no merging, no
    touching anything outside the PR's own diff.
"""

from __future__ import annotations

from .github_client import PullRequest

_SHARED_RULES = """
Hard constraints — these are not suggestions:
- Work ONLY on the branch `{head_ref}`. Never push to `{base_ref}`.
- Do NOT merge the pull request, approve it, or close it. A human decides that.
- Do NOT change files unrelated to making this PR mergeable. No drive-by
  refactors, no reformatting untouched files, no dependency bumps.
- If you cannot finish safely, stop and report `outcome: "failed"` with a clear
  explanation. A truthful failure is worth far more than a green build that
  quietly dropped someone's work.
"""

_CONFLICT_TASK = """
This pull request has merge conflicts against `{base_ref}` and cannot be merged.

Goal: the branch merges cleanly into `{base_ref}` with the contributor's intent
fully preserved.

What to do:
1. Read the PR diff and its description first. Understand what the contributor
   was trying to achieve BEFORE you look at a single conflict marker.
2. Rebase `{head_ref}` onto the latest `{base_ref}`.
3. Resolve each conflict on the merits. For every conflict, the question is
   "what did each side intend, and what does the combination look like?" — not
   "which side is newer".
4. If `{base_ref}` has refactored or moved code the PR touches, port the
   contributor's change to its new home rather than reverting it.
5. Run the relevant tests and linters for the files you touched. Fix anything
   your resolution broke.
6. Force-push the rebased branch to `{head_ref}` only.

The failure mode to avoid above all: "resolving" a conflict by taking
`{base_ref}`'s side wholesale and deleting the contributor's change. That looks
like success — clean rebase, green CI — while silently discarding the entire
point of the PR. If a conflict genuinely cannot be reconciled, report
`outcome: "failed"` and say which hunk defeated you and why.
"""

_CI_TASK = """
This pull request has no merge conflicts, but at least one required check is
failing, so it cannot be merged.

Goal: the failing checks pass, with the contributor's intent preserved.

What to do:
1. Read the PR diff and description, then fetch the failing check logs and
   identify the actual root cause. Do not guess from the job name.
2. Classify the failure before fixing it:
   - The PR genuinely broke something  -> fix the PR's code.
   - `{base_ref}` moved underneath the PR (renamed API, changed fixture,
     stricter lint rule) -> adapt the PR to the new reality.
   - The test is flaky and unrelated to this diff -> do NOT paper over it.
     Report `outcome: "failed"` naming the flaky test, so a human can decide.
3. Never make a check pass by weakening it. Do not delete or skip a failing
   test, loosen an assertion, or add a lint suppression to silence a real
   finding. If that seems like the only route, that is a `failed` outcome.
4. Run the affected tests locally to confirm your fix before pushing.
5. Push to `{head_ref}` only.
"""


def build_prompt(pr: PullRequest, repo: str, blocker: str) -> str:
    """Assemble the session prompt for one blocked PR."""
    task = _CONFLICT_TASK if blocker == "conflict" else _CI_TASK
    fmt = {"head_ref": pr.head_ref, "base_ref": pr.base_ref}

    return f"""You are unblocking a stalled pull request in `{repo}`.

PR #{pr.number}: {pr.title}
Author:      @{pr.author}
Branch:      `{pr.head_ref}` -> `{pr.base_ref}`
Open for:    {pr.age_days:.0f} days
Link:        {pr.html_url}

Context you should know: this PR has been open for {pr.age_days:.0f} days. The
contributor has almost certainly moved on, so nobody is coming to answer
questions. The work itself was reviewed and wanted — it stalled on mechanics,
not on merit. Your job is to restore it to a state where a maintainer can make a
decision about it.
{task.format(**fmt)}{_SHARED_RULES.format(**fmt)}

When you are done, return structured output with `outcome` set to `succeeded`,
`failed`, or `not_needed` (use `not_needed` if the PR turns out not to be blocked
at all), plus a one or two sentence `summary` a reviewer can read at a glance.
"""


def build_issue_body(pr: PullRequest, repo: str, blocker: str, label: str) -> str:
    """Body of the tracking issue filed for a blocked PR."""
    human = {
        "conflict": "Merge conflicts against the base branch (`mergeable_state: dirty`)",
        "failing_ci": "A required check is failing (`mergeable_state: unstable`)",
    }[blocker]

    return f"""### Blocked pull request

**PR:** {pr.html_url} — {pr.title}
**Author:** @{pr.author}
**Open for:** {pr.age_days:.0f} days
**Blocker:** {human}

### Why this was picked up

This PR cannot be merged for a mechanical reason rather than a review decision.
It was detected automatically by the PR-unblocker on repository activity; no
human filed this issue.

### What happens next

Applying the `{label}` label dispatches a Devin session to rebase and/or repair
this branch. Devin pushes to `{pr.head_ref}` only — it will not merge, approve,
or close the PR. A maintainer still makes the final call.

<sub>Tracked automatically · repo `{repo}` · PR #{pr.number}</sub>
"""
