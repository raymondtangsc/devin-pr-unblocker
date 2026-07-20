"""Tests for the unblocker pipeline.

Focus is on the things that would cause real damage if wrong: the upstream-repo
guardrail, dispatch idempotency (duplicate sessions cost money), and correct
classification of agent outcomes.
"""

from __future__ import annotations

import asyncio

import pytest

from app import store as st
from app.config import BLOCKED_REPOS, Config, ConfigError
from app.devin_client import MockDevinClient, Session, _parse_session
from app.github_client import MockGitHubClient
from app.orchestrator import Orchestrator, _read_outcome
from app.store import Store

FORK = "raymondtangsc/superset"


def make_cfg(**over) -> Config:
    base = dict(
        devin_api_key="",
        devin_org_id="",
        devin_api_base="https://api.devin.ai/v3",
        devin_mode="mock",
        devin_max_acu=5,
        github_token="",
        github_repo=FORK,
        github_webhook_secret="",
        trigger_label="devin-unblock",
        max_dispatches_per_event=3,
        db_path=":memory:",
    )
    base.update(over)
    return Config(**base)


@pytest.fixture
def orch() -> Orchestrator:
    cfg = make_cfg()
    return Orchestrator(cfg, Store(":memory:"), MockGitHubClient(), MockDevinClient())


# ------------------------------------------------------------------ guardrails


@pytest.mark.parametrize("repo", sorted(BLOCKED_REPOS))
def test_upstream_repo_is_refused(repo: str) -> None:
    cfg = make_cfg()
    with pytest.raises(ConfigError, match="Refusing to operate on upstream"):
        cfg.assert_repo_allowed(repo)


def test_unrelated_repo_is_refused() -> None:
    cfg = make_cfg()
    with pytest.raises(ConfigError, match="does not match configured"):
        cfg.assert_repo_allowed("someone/else")


def test_configured_fork_is_allowed() -> None:
    make_cfg().assert_repo_allowed(FORK)


def test_detect_refuses_upstream(orch: Orchestrator) -> None:
    with pytest.raises(ConfigError):
        asyncio.run(orch.detect("apache/superset"))


def test_live_mode_without_org_id_fails_loudly() -> None:
    cfg = make_cfg(devin_mode="live", devin_api_key="cog_x", devin_org_id="")
    with pytest.raises(ConfigError, match="DEVIN_ORG_ID"):
        _ = cfg.live_devin


def test_auto_mode_degrades_to_mock_without_credentials() -> None:
    assert make_cfg(devin_mode="auto").live_devin is False


def test_auto_mode_goes_live_when_both_present() -> None:
    cfg = make_cfg(devin_mode="auto", devin_api_key="cog_x", devin_org_id="org_y")
    assert cfg.live_devin is True


# ------------------------------------------------------------------ detection


def test_detect_finds_only_blocked_non_draft_prs(orch: Orchestrator) -> None:
    blocked = asyncio.run(orch.detect(FORK))
    numbers = {pr.number for pr, _ in blocked}

    # Fixture holds 10 dirty + 9 unstable + 3 clean; 4 of the blocked are drafts.
    assert all(not pr.draft for pr, _ in blocked)
    assert all(b in {"conflict", "failing_ci"} for _, b in blocked)
    # #24949 is clean -> must not appear.
    assert 24949 not in numbers
    # #28627 is dirty and not a draft -> must appear.
    assert 28627 in numbers


def test_detect_sorts_oldest_first(orch: Orchestrator) -> None:
    blocked = asyncio.run(orch.detect(FORK))
    ages = [pr.age_days for pr, _ in blocked]
    assert ages == sorted(ages, reverse=True)


def test_blocker_classification() -> None:
    gh = MockGitHubClient()
    prs = asyncio.run(gh.list_open_prs(FORK))
    by_num = {p.number: p for p in prs}
    assert by_num[28627].blocker == "conflict"  # dirty
    assert by_num[22604].blocker == "failing_ci"  # unstable
    assert by_num[24949].blocker is None  # clean


# ------------------------------------------------------------- record/dispatch


def test_record_files_one_issue_per_pr(orch: Orchestrator) -> None:
    pr = asyncio.run(orch.github.get_pr(FORK, 28627))
    first = asyncio.run(orch.record(FORK, pr, "conflict"))
    second = asyncio.run(orch.record(FORK, pr, "conflict"))

    assert first is not None and first.issue_number is not None
    assert second is None, "re-detecting a tracked PR must not file a second issue"
    assert len(orch.github.issues) == 1


def test_issue_carries_pr_number_for_label_trigger(orch: Orchestrator) -> None:
    pr = asyncio.run(orch.github.get_pr(FORK, 28627))
    asyncio.run(orch.record(FORK, pr, "conflict"))
    from app.main import _pr_number_from_issue

    issue = orch.github.issues[0]
    assert _pr_number_from_issue(issue) == 28627


def test_dispatch_is_idempotent(orch: Orchestrator) -> None:
    pr = asyncio.run(orch.github.get_pr(FORK, 28627))
    asyncio.run(orch.record(FORK, pr, "conflict"))

    first = asyncio.run(orch.dispatch(FORK, 28627))
    second = asyncio.run(orch.dispatch(FORK, 28627))

    assert first is not None and first.session_id
    assert second is not None
    assert second.session_id == first.session_id, "must not open a second session"


def test_dispatch_skips_pr_that_unblocked_itself(orch: Orchestrator) -> None:
    pr = asyncio.run(orch.github.get_pr(FORK, 24949))  # clean
    orch.store.upsert_detected(
        st.WorkItem(24949, FORK, pr.title, pr.author, "conflict", 1.0, st.DETECTED)
    )
    item = asyncio.run(orch.dispatch(FORK, 24949))
    assert item is not None and item.state == st.SKIPPED
    assert item.session_id is None


def test_dispatch_cap_defers_the_rest(orch: Orchestrator) -> None:
    result = asyncio.run(orch.handle_repo_event(FORK, reason="test"))
    assert len(result["dispatched"]) == orch.cfg.max_dispatches_per_event
    assert result["deferred"], "surplus work must be queued, not silently dropped"
    assert result["newly_tracked"] == result["scanned_blocked"]


# -------------------------------------------------------------------- outcomes


def test_read_outcome_prefers_structured_output() -> None:
    s = Session("s1", "u", "finished", structured_output={"outcome": "failed", "summary": "nope"})
    assert _read_outcome(s) == ("failed", "nope")


def test_read_outcome_falls_back_to_status() -> None:
    outcome, summary = _read_outcome(Session("s1", "u", "finished"))
    assert outcome == "succeeded"
    assert summary

    outcome, _ = _read_outcome(Session("s1", "u", "expired"))
    assert outcome == "failed"


def test_not_needed_maps_to_skipped(orch: Orchestrator) -> None:
    s = Session("s", "u", "finished", structured_output={"outcome": "not_needed", "summary": "fine"})
    assert _read_outcome(s)[0] == "not_needed"


def test_parse_session_handles_missing_fields() -> None:
    s = _parse_session({"session_id": "x", "url": "u", "status": "running"})
    assert s.session_id == "x" and s.acus_consumed == 0.0
    assert not s.is_terminal


# ---------------------------------------------------------------- end-to-end


def test_full_pipeline_reaches_terminal_states(orch: Orchestrator) -> None:
    asyncio.run(orch.handle_repo_event(FORK, reason="test"))

    async def drain() -> None:
        for _ in range(12):
            if not orch.store.by_state(st.DISPATCHED, st.RUNNING):
                return
            await orch.reconcile()

    asyncio.run(drain())

    dispatched = [i for i in orch.store.all_items() if i.session_id]
    assert dispatched, "expected at least one dispatched item"
    assert all(i.state in st.TERMINAL for i in dispatched)

    m = orch.store.metrics()
    assert m["total_tracked"] == len(orch.store.all_items())
    assert m["success_rate"] is not None
    assert m["total_acus"] > 0


def test_metrics_are_safe_on_empty_store() -> None:
    m = Store(":memory:").metrics()
    assert m["total_tracked"] == 0
    assert m["success_rate"] is None
    assert m["median_unblock_seconds"] is None


def test_mock_failure_path_is_deterministic() -> None:
    """A demo must exercise the failure path every run, not statistically."""
    devin = MockDevinClient(fail_every=3, polls_to_finish=1)

    async def run() -> list[str]:
        outcomes = []
        for _ in range(6):
            s = await devin.create_session("p", title="t", tags=[], max_acu=5)
            done = await devin.get_session(s.session_id)
            outcomes.append(_read_outcome(done)[0])
        return outcomes

    outcomes = asyncio.run(run())
    assert outcomes == [
        "succeeded", "succeeded", "failed",
        "succeeded", "succeeded", "failed",
    ]


def test_blocked_session_is_terminal_and_escalates() -> None:
    """Devin's 'blocked' means it stopped to ask a human.

    Nobody is watching an unattended session, so this must terminate the work
    item rather than polling forever.
    """
    s = Session("s", "u", "blocked")
    assert s.is_terminal, "blocked must not poll forever"
    assert not s.is_success
    assert s.needs_human
    outcome, summary = _read_outcome(s)
    assert outcome == "failed"
    assert "human" in summary.lower()


def test_waiting_for_user_is_terminal_despite_running_status() -> None:
    """Observed live: a finished session still reports status='running'.

    The only signal that it is waiting on a human is status_detail. Reading the
    coarse status alone means these items poll forever.
    """
    s = _parse_session(
        {
            "session_id": "s",
            "url": "u",
            "status": "running",
            "status_detail": "waiting_for_user",
        }
    )
    assert s.status_detail == "waiting_for_user"
    assert s.needs_human, "must escalate rather than poll forever"
    assert s.is_terminal
    outcome, summary = _read_outcome(s)
    assert outcome == "failed"
    assert "waiting_for_user" in summary


def test_actively_working_session_is_not_terminal() -> None:
    s = _parse_session(
        {"session_id": "s", "url": "u", "status": "running", "status_detail": "working"}
    )
    assert not s.needs_human and not s.is_terminal


def test_suspended_by_inactivity_is_terminal() -> None:
    s = _parse_session(
        {"session_id": "s", "url": "u", "status": "suspended", "status_detail": "inactivity"}
    )
    assert s.needs_human and s.is_terminal


def test_detect_hydrates_prs_missing_mergeable_state() -> None:
    """The GitHub list endpoint omits mergeable_state; detect must fill it in.

    Without hydration every PR reads as 'unknown' and nothing is ever detected —
    which is exactly how this failed against the live API.
    """
    from app.github_client import PullRequest

    class ListWithoutState(MockGitHubClient):
        """Mimics the real list endpoint: no mergeable_state on listed PRs."""

        def __init__(self) -> None:
            super().__init__()
            self.hydrated: list[int] = []

        async def list_open_prs(self, repo: str):
            out = []
            for pr in self._prs.values():
                stripped = PullRequest(**{**pr.__dict__, "mergeable_state": "unknown"})
                out.append(stripped)
            return out

        async def get_pr(self, repo: str, number: int):
            self.hydrated.append(number)
            return await super().get_pr(repo, number)

    gh = ListWithoutState()
    o = Orchestrator(make_cfg(), Store(":memory:"), gh, MockDevinClient())
    blocked = asyncio.run(o.detect(FORK))

    assert gh.hydrated, "must fetch full PRs to learn mergeable_state"
    assert blocked, "hydration must recover the blocked PRs"
    assert 28627 in {pr.number for pr, _ in blocked}


# ------------------------------------------------- outcome verification


def _dispatched_item(orch: Orchestrator, pr_number: int, blocker: str = "conflict"):
    pr = asyncio.run(orch.github.get_pr(FORK, pr_number))
    orch.store.upsert_detected(
        st.WorkItem(pr_number, FORK, pr.title, pr.author, blocker, 9.0, st.DETECTED)
    )
    orch.store.set_dispatched(pr_number, "sess-1", "https://app.devin.ai/sessions/sess-1")
    return orch.store.get(pr_number)


class _ClaimsSuccess(MockDevinClient):
    """A session that reports success regardless of what it actually did."""

    async def get_session(self, session_id: str) -> Session:
        return Session(
            session_id, "u", "finished",
            acus_consumed=3.0,
            structured_output={"outcome": "succeeded", "summary": "Resolved 6 conflicts."},
        )


def test_false_success_is_caught_by_verification() -> None:
    """Observed live: an agent reported success while the PR stayed `dirty`.

    A self-report is a claim. If GitHub still says the PR is blocked, the item
    must not be counted as a win.
    """
    orch = Orchestrator(make_cfg(), Store(":memory:"), MockGitHubClient(), _ClaimsSuccess())
    _dispatched_item(orch, 28627)  # fixture PR is `dirty` and stays that way

    asyncio.run(orch.reconcile())

    item = orch.store.get(28627)
    assert item.state == st.FAILED, "unverified success must not count as success"
    assert "still `dirty`" in (item.detail or "")
    assert orch.store.metrics()["success_rate"] == 0.0


def test_genuine_success_passes_verification() -> None:
    """The same claim, but GitHub confirms the PR is no longer conflicted."""
    gh = MockGitHubClient()
    orch = Orchestrator(make_cfg(), Store(":memory:"), gh, _ClaimsSuccess())
    _dispatched_item(orch, 28627)

    # Simulate the rebase landing: the PR is no longer dirty.
    from app.github_client import PullRequest

    gh._prs[28627] = PullRequest(
        **{**gh._prs[28627].__dict__, "mergeable_state": "clean", "mergeable": True}
    )
    asyncio.run(orch.reconcile())

    item = orch.store.get(28627)
    assert item.state == st.SUCCEEDED
    assert orch.store.metrics()["success_rate"] == 1.0


def test_conflict_resolved_but_ci_pending_still_counts() -> None:
    """A rebased PR whose CI is now pending traded blockers -- that is progress.

    The next sweep picks up the CI blocker on its own merits.
    """
    gh = MockGitHubClient()
    orch = Orchestrator(make_cfg(), Store(":memory:"), gh, _ClaimsSuccess())
    _dispatched_item(orch, 28627, blocker="conflict")

    from app.github_client import PullRequest

    gh._prs[28627] = PullRequest(
        **{**gh._prs[28627].__dict__, "mergeable_state": "unstable", "mergeable": True}
    )
    asyncio.run(orch.reconcile())
    assert orch.store.get(28627).state == st.SUCCEEDED
