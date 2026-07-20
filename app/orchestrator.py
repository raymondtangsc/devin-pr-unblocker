"""The pipeline: detect blocked PRs, record them, dispatch Devin, track outcomes.

Flow:

    repository event
        -> detect()      classify open PRs by mergeable_state   (deterministic)
        -> record()      file a tracking issue per blocked PR
        -> dispatch()    one Devin session per labelled issue
        -> reconcile()   poll sessions to a terminal state

Detection is deliberately free of any model call — it is an API query and a
dictionary lookup. Devin is spent only on the part that needs judgement.
"""

from __future__ import annotations

import asyncio
import logging

from . import store as st
from .devin_client import DevinClient
from .github_client import GitHubClient, GitHubSetupError, PullRequest
from .prompts import build_issue_body, build_prompt
from .store import Store, WorkItem

log = logging.getLogger("unblocker")


class Orchestrator:
    def __init__(
        self, cfg, store: Store, github: GitHubClient, devin: DevinClient
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.github = github
        self.devin = devin

    # ------------------------------------------------------------- detection

    async def detect(self, repo: str | None = None) -> list[tuple[PullRequest, str]]:
        """Return open PRs that are mechanically blocked.

        Drafts are excluded: their author is still working, so a red build is
        expected and rebasing under them would be rude.
        """
        repo = repo or self.cfg.github_repo
        self.cfg.assert_repo_allowed(repo)

        prs = await self.github.list_open_prs(repo)

        # GitHub's list endpoint does NOT include mergeable_state -- it is
        # computed lazily and only appears on the single-PR endpoint. Every PR
        # therefore arrives as "unknown" and must be hydrated individually.
        # Bounded concurrency keeps a 376-PR sweep from hammering the API.
        candidates = [pr for pr in prs if not pr.draft]
        hydrated = await self._hydrate(repo, candidates)

        # Quiet-period gate: a PR with recent activity is someone's work in
        # progress. Touching it -- worse, force-pushing onto it -- would collide
        # with the author's local branch. Rot takes weeks; we can wait days.
        quiet = [
            pr
            for pr in hydrated
            if not pr.draft and pr.quiet_days >= self.cfg.min_quiet_days
        ]
        active = sum(1 for pr in hydrated if not pr.draft) - len(quiet)
        if active:
            self.store.log(
                "quiet_gate",
                f"{active} blocked-or-candidate PR(s) skipped: active within "
                f"{self.cfg.min_quiet_days} day(s); will pick up once quiet",
                active=active,
            )

        classified = await asyncio.gather(*(self.classify(repo, pr) for pr in quiet))
        blocked = [
            (pr, blocker)
            for pr, blocker in zip(quiet, classified)
            if blocker is not None
        ]
        blocked.sort(key=lambda pair: pair[0].age_days, reverse=True)

        awaiting_review = sum(
            1
            for pr, blocker in zip(quiet, classified)
            if blocker is None and pr.mergeable_state == "blocked"
        )
        self.store.log(
            "detect",
            f"scanned {len(prs)} open PRs, {len(blocked)} mechanically blocked, "
            f"{awaiting_review} awaiting human review (skipped)",
            scanned=len(prs),
            blocked=len(blocked),
            awaiting_review=awaiting_review,
        )
        return blocked

    async def classify(self, repo: str, pr: PullRequest) -> str | None:
        """Decide whether a PR is blocked on something mechanical.

        `blocked` is 58% of Superset's open PRs and is genuinely ambiguous: it
        covers both "a required check is red" (mechanical, ours) and "waiting
        for a human to review" (not ours). GitHub does not distinguish them in
        `mergeable_state`, so we look at the check runs.

        Defaulting to "not ours" is deliberate. Dispatching an agent at a PR
        that is merely waiting for a reviewer wastes money and, worse, implies
        the tool is trying to route around code review.
        """
        if not pr.needs_check_inspection:
            return pr.blocker

        try:
            failing = await self.github.failing_checks(repo, pr.head_sha)
        except Exception as exc:
            log.warning("could not read checks for PR #%s: %s", pr.number, exc)
            return None  # cannot prove it is mechanical -> leave it alone

        if failing:
            self.store.log(
                "classified",
                f"PR #{pr.number} is blocked by failing checks: "
                f"{', '.join(failing[:4])}",
                pr_number=pr.number,
            )
            return "failing_ci"
        return None  # blocked, but nothing red -> awaiting human review

    async def _hydrate(
        self, repo: str, prs: list[PullRequest], concurrency: int = 8
    ) -> list[PullRequest]:
        """Fill in mergeable_state for PRs the list endpoint left as 'unknown'.

        A PR that fails to hydrate is dropped rather than guessed at: acting on
        a stale or missing merge state is how you dispatch an agent at a PR that
        was fine all along.
        """
        needs = [pr for pr in prs if pr.mergeable_state == "unknown"]
        if not needs:
            return prs

        sem = asyncio.Semaphore(concurrency)

        async def one(pr: PullRequest) -> PullRequest | None:
            async with sem:
                try:
                    return await self.github.get_pr(repo, pr.number)
                except Exception as exc:
                    log.warning("could not hydrate PR #%s: %s", pr.number, exc)
                    return None

        fetched = await asyncio.gather(*(one(pr) for pr in needs))
        by_number = {pr.number: pr for pr in fetched if pr is not None}

        failed = len(needs) - len(by_number)
        if failed:
            self.store.log(
                "hydrate_incomplete",
                f"{failed} PR(s) could not be checked and were skipped this pass",
                failed=failed,
            )

        return [
            by_number.get(pr.number, pr)
            for pr in prs
            if pr.mergeable_state != "unknown" or pr.number in by_number
        ]

    # --------------------------------------------------------------- record

    async def record(self, repo: str, pr: PullRequest, blocker: str) -> WorkItem | None:
        """File a tracking issue for a blocked PR, once."""
        self.cfg.assert_repo_allowed(repo)

        item = WorkItem(
            pr_number=pr.number,
            repo=repo,
            title=pr.title,
            author=pr.author,
            blocker=blocker,
            pr_age_days=pr.age_days,
            state=st.DETECTED,
        )
        if not self.store.upsert_detected(item):
            return None  # already tracked; do not file a duplicate issue

        issue_number = await self.github.create_issue(
            repo,
            title=f"Unblock PR #{pr.number}: {pr.title[:70]}",
            body=build_issue_body(pr, repo, blocker, self.cfg.trigger_label),
            labels=[self.cfg.trigger_label, f"blocker:{blocker}"],
        )
        self.store.set_issue(pr.number, issue_number)
        self.store.log(
            "issue_filed",
            f"filed issue #{issue_number} for PR #{pr.number} ({blocker})",
            pr_number=pr.number,
            issue=issue_number,
        )
        return self.store.get(pr.number)

    # ------------------------------------------------------------- dispatch

    async def dispatch(
        self, repo: str, pr_number: int, force: bool = False
    ) -> WorkItem | None:
        """Start a Devin session for one blocked PR.

        ``force=True`` bypasses the quiet-period gate -- used by the manual
        label path, where a human explicitly pointing the teammate at a PR is
        consent to work on it.
        """
        self.cfg.assert_repo_allowed(repo)

        item = self.store.get(pr_number)
        if item is None:
            log.warning("dispatch called for untracked PR #%s", pr_number)
            return None
        if item.session_id:
            # Idempotency: re-labelling an issue must not start a second session.
            self.store.log(
                "dispatch_skipped",
                f"PR #{pr_number} already has session {item.session_id}",
                pr_number=pr_number,
            )
            return item

        pr = await self.github.get_pr(repo, pr_number)
        if not force and pr.quiet_days < self.cfg.min_quiet_days:
            # Author is actively pushing; stay out of their way. The item stays
            # queued and a later sweep re-tries once the branch goes quiet.
            self.store.log(
                "quiet_deferred",
                f"PR #{pr_number} active {pr.quiet_days:.1f}d ago (< "
                f"{self.cfg.min_quiet_days}d quiet); deferring dispatch",
                pr_number=pr_number,
            )
            return item
        blocker = await self.classify(repo, pr)
        if blocker is None:
            reason = (
                "PR is waiting on human review, not on mechanical work."
                if pr.mergeable_state == "blocked"
                else "PR is no longer blocked; nothing to do."
            )
            self.store.set_state(pr_number, st.SKIPPED, detail=reason)
            self.store.log("skipped", f"PR #{pr_number}: {reason}", pr_number=pr_number)
            return self.store.get(pr_number)

        prompt = build_prompt(pr, repo, blocker)
        session = await self.devin.create_session(
            prompt,
            title=f"Unblock {repo}#{pr.number}",
            tags=["pr-unblocker", f"blocker:{blocker}", f"pr:{pr.number}"],
            max_acu=self.cfg.devin_max_acu,
        )
        self.store.set_dispatched(pr_number, session.session_id, session.url)
        self.store.log(
            "dispatched",
            f"session {session.session_id} started for PR #{pr_number}",
            pr_number=pr_number,
            session=session.session_id,
        )

        if item.issue_number:
            await self.github.comment(
                repo,
                item.issue_number,
                f"Devin session started: {session.url}\n\n"
                f"Blocker: `{blocker}` · ACU ceiling: {self.cfg.devin_max_acu}",
            )
        return self.store.get(pr_number)

    # ------------------------------------------------------------ reconcile

    async def reconcile(self) -> int:
        """Poll every in-flight session and settle the ones that finished.

        Returns the number of items that reached a terminal state this pass.
        """
        in_flight = self.store.by_state(st.DISPATCHED, st.RUNNING)
        settled = 0

        for item in in_flight:
            if not item.session_id:
                continue
            try:
                session = await self.devin.get_session(item.session_id)
            except Exception as exc:  # network, auth, unknown session
                log.warning("poll failed for %s: %s", item.session_id, exc)
                self.store.log(
                    "poll_error",
                    f"could not poll session {item.session_id}: {exc}",
                    pr_number=item.pr_number,
                )
                continue

            if not session.is_terminal:
                if item.state != st.RUNNING:
                    self.store.set_state(
                        item.pr_number, st.RUNNING, acus=session.acus_consumed
                    )
                continue

            outcome, summary = _read_outcome(session)
            state = st.SUCCEEDED if outcome == "succeeded" else st.FAILED
            if outcome == "not_needed":
                state = st.SKIPPED

            # An agent's self-report is a claim, not evidence. Observed live: a
            # session reported outcome="succeeded" with a detailed description of
            # resolving six conflicts, while the branch head never moved and the
            # PR stayed `dirty`. Confirm against GitHub before counting a win --
            # otherwise the success rate measures the agent's confidence rather
            # than its results.
            if state == st.SUCCEEDED:
                verified, why = await self._verify_unblocked(item)
                if not verified:
                    state = st.FAILED
                    summary = f"Reported success, but {why}. Escalating. — {summary}"
                    self.store.log(
                        "verification_failed",
                        f"PR #{item.pr_number}: agent claimed success but {why}",
                        pr_number=item.pr_number,
                    )

            self.store.set_state(
                item.pr_number, state, detail=summary, acus=session.acus_consumed
            )
            self.store.log(
                state,
                f"PR #{item.pr_number}: {summary[:120]}",
                pr_number=item.pr_number,
                acus=session.acus_consumed,
            )
            settled += 1

            if item.issue_number:
                verdict = {
                    st.SUCCEEDED: "Unblocked — ready for review",
                    st.FAILED: "Needs a human",
                    st.SKIPPED: "No longer blocked",
                }[state]
                try:
                    await self.github.comment(
                        item.repo,
                        item.issue_number,
                        f"**{verdict}**\n\n{summary}\n\n"
                        f"Session: {item.session_url} · "
                        f"ACUs: {session.acus_consumed}",
                    )
                except Exception as exc:
                    log.warning("comment failed on #%s: %s", item.issue_number, exc)

        return settled

    async def _verify_unblocked(self, item: WorkItem) -> tuple[bool, str]:
        """Confirm against GitHub that the PR's original blocker is actually gone.

        Returns (verified, reason_if_not). A PR that traded one blocker for
        another still counts as progress on the blocker we dispatched for: a
        rebased branch whose CI is now pending is a real step forward, and the
        next sweep will pick up the CI blocker on its own merits.
        """
        try:
            pr = await self.github.get_pr(item.repo, item.pr_number)
        except Exception as exc:
            # Cannot confirm, so do not claim a win.
            return False, f"the PR could not be re-checked ({exc})"

        if item.blocker == "conflict":
            if pr.mergeable_state == "dirty":
                return False, "the PR is still `dirty` (conflicts remain)"
            if pr.mergeable is False:
                return False, "GitHub still reports the PR as unmergeable"
            return True, ""

        if item.blocker == "failing_ci":
            if pr.mergeable_state in ("unstable", "blocked"):
                failing = await self.github.failing_checks(item.repo, pr.head_sha)
                if failing:
                    return False, f"checks are still failing ({', '.join(failing[:3])})"
            return True, ""

        if item.blocker == "stale_base":
            if pr.mergeable_state == "behind":
                return False, "the branch is still behind the base"
            return True, ""

        return True, ""

    # ------------------------------------------------------- composite flows

    async def handle_repo_event(self, repo: str, reason: str) -> dict:
        """Full sweep triggered by repository activity."""
        blocked = await self.detect(repo)
        newly_filed: list[int] = []

        for pr, blocker in blocked:
            try:
                item = await self.record(repo, pr, blocker)
            except GitHubSetupError as exc:
                # A setup problem affects every PR equally; failing the whole
                # sweep once is clearer than 16 identical errors.
                self.store.log("setup_error", str(exc))
                log.error("%s", exc)
                return {
                    "reason": reason,
                    "error": str(exc),
                    "scanned_blocked": len(blocked),
                    "newly_tracked": 0,
                    "dispatched": [],
                    "deferred": [],
                }
            if item is not None:
                newly_filed.append(pr.number)

        # Cap auto-dispatch per event. Without a ceiling, the first run against a
        # 376-PR backlog would open hundreds of concurrent sessions. The queue
        # includes items deferred by earlier events -- otherwise anything past
        # the cap would wait forever for a manual label.
        queued = [
            i.pr_number
            for i in self.store.by_state(st.ISSUE_FILED)
            if not i.session_id
        ]
        # newly filed first (freshest detection), then the standing queue
        ordered = newly_filed + [n for n in queued if n not in newly_filed]
        to_dispatch = ordered[: self.cfg.max_dispatches_per_event]
        deferred = ordered[self.cfg.max_dispatches_per_event :]

        results = await asyncio.gather(
            *(self.dispatch(repo, n) for n in to_dispatch), return_exceptions=True
        )
        dispatched = [
            n for n, r in zip(to_dispatch, results) if not isinstance(r, Exception)
        ]
        for n, r in zip(to_dispatch, results):
            if isinstance(r, Exception):
                self.store.set_state(n, st.FAILED, detail=f"dispatch error: {r}")
                self.store.log("dispatch_error", f"PR #{n}: {r}", pr_number=n)

        if deferred:
            self.store.log(
                "deferred",
                f"{len(deferred)} PRs queued behind the per-event dispatch cap "
                f"of {self.cfg.max_dispatches_per_event}",
                deferred=deferred,
            )

        return {
            "reason": reason,
            "scanned_blocked": len(blocked),
            "newly_tracked": len(newly_filed),
            "dispatched": dispatched,
            "deferred": deferred,
        }


def _read_outcome(session) -> tuple[str, str]:
    """Extract (outcome, summary) from a finished session.

    Prefers the structured output the session was asked to produce; falls back to
    the coarse session status when it is absent, which is what happens if the
    agent hit its ACU ceiling before reporting.
    """
    out = session.structured_output or {}
    outcome = str(out.get("outcome") or "").lower()
    summary = str(out.get("summary") or "").strip()

    if outcome in {"succeeded", "failed", "not_needed"}:
        return outcome, summary or "(no summary returned)"

    if session.needs_human:
        why = f" ({session.status_detail})" if session.status_detail else ""
        return "failed", summary or (
            f"Session stopped for human input (status '{session.status}'{why})."
        )
    if session.is_success:
        return "succeeded", summary or "Session finished without structured output."
    return "failed", summary or f"Session ended with status '{session.status}'."
