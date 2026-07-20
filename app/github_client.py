"""GitHub REST client, plus a fixture-backed mock.

Only the handful of calls this system needs: list open PRs, read a PR's merge
state, file an issue, comment, and label. Every write path funnels through
``_guard`` so a misconfigured repo is rejected before the request leaves the
process.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

API = "https://api.github.com"

# GitHub's mergeable_state values, and whether we can do anything about them.
#
# Measured across all 376 open PRs in apache/superset:
#   blocked   58.2%  branch protection unsatisfied -- AMBIGUOUS, see below
#   dirty     38.6%  merge conflicts
#   unstable   1.9%  a non-required check is failing
#   clean      1.3%  ready
#   behind     0%    base moved and the repo requires up-to-date branches
#
# `blocked` is the largest bucket and cannot be classified from this field
# alone: it covers both "waiting for a human to review" (which we must not
# touch) and "a required check is red" (which is mechanical). Resolving it
# needs the check runs -- see Orchestrator.classify.
UNAMBIGUOUS_BLOCKERS = {
    "dirty": "conflict",
    "unstable": "failing_ci",
    "behind": "stale_base",
}

# Check-run conclusions that mean a human has to intervene.
FAILING_CONCLUSIONS = {"failure", "timed_out", "action_required", "startup_failure"}


class GitHubSetupError(RuntimeError):
    """A misconfiguration the operator must fix, not a transient failure."""


class IssuesDisabled(GitHubSetupError):
    pass


class GitHubPermissionError(GitHubSetupError):
    pass


@dataclass
class PullRequest:
    number: int
    title: str
    author: str
    created_at: str
    updated_at: str
    mergeable_state: str
    draft: bool
    head_ref: str
    base_ref: str
    html_url: str
    head_sha: str = ""
    # GitHub's boolean verdict, distinct from the coarse mergeable_state string.
    # None while GitHub is still computing it.
    mergeable: bool | None = None

    @property
    def age_days(self) -> float:
        created = time.mktime(time.strptime(self.created_at, "%Y-%m-%dT%H:%M:%SZ"))
        return round((time.time() - created) / 86400, 1)

    @property
    def quiet_days(self) -> float:
        """Days since the PR last saw any activity (pushes, comments, edits)."""
        updated = time.mktime(time.strptime(self.updated_at, "%Y-%m-%dT%H:%M:%SZ"))
        return round((time.time() - updated) / 86400, 2)

    @property
    def blocker(self) -> str | None:
        """Why this PR cannot merge, when that is knowable from this field alone.

        Returns None for `blocked`, which is genuinely ambiguous -- use
        Orchestrator.classify, which consults the check runs.
        """
        return UNAMBIGUOUS_BLOCKERS.get(self.mergeable_state)

    @property
    def needs_check_inspection(self) -> bool:
        """`blocked` could be a red required check or a missing human review."""
        return self.mergeable_state == "blocked"


class GitHubClient(Protocol):
    async def list_open_prs(self, repo: str) -> list[PullRequest]: ...
    async def get_pr(self, repo: str, number: int) -> PullRequest: ...
    async def create_issue(
        self, repo: str, title: str, body: str, labels: list[str]
    ) -> int: ...
    async def comment(self, repo: str, issue_number: int, body: str) -> None: ...
    async def failing_checks(self, repo: str, sha: str) -> list[str]: ...
    @property
    def mode(self) -> str: ...


class LiveGitHubClient:
    def __init__(self, token: str, cfg) -> None:
        self._token = token
        self._cfg = cfg

    @property
    def mode(self) -> str:
        return "live"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _guard(self, repo: str) -> None:
        self._cfg.assert_repo_allowed(repo)

    async def list_open_prs(self, repo: str) -> list[PullRequest]:
        self._guard(repo)
        out: list[PullRequest] = []
        async with httpx.AsyncClient(timeout=30) as client:
            for page in range(1, 5):
                r = await client.get(
                    f"{API}/repos/{repo}/pulls",
                    headers=self._headers,
                    params={"state": "open", "per_page": 100, "page": page},
                )
                r.raise_for_status()
                batch = r.json()
                out.extend(_parse_pr(p) for p in batch)
                if len(batch) < 100:
                    break
        return out

    async def get_pr(self, repo: str, number: int) -> PullRequest:
        self._guard(repo)
        # mergeable_state is only computed on the single-PR endpoint, and GitHub
        # computes it lazily -- a fresh PR can report "unknown" for a few seconds.
        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in range(3):
                r = await client.get(
                    f"{API}/repos/{repo}/pulls/{number}", headers=self._headers
                )
                r.raise_for_status()
                data = r.json()
                if data.get("mergeable_state") != "unknown":
                    return _parse_pr(data)
                await _sleep(1.5 * (attempt + 1))
            return _parse_pr(data)

    async def create_issue(
        self, repo: str, title: str, body: str, labels: list[str]
    ) -> int:
        self._guard(repo)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{API}/repos/{repo}/issues",
                headers=self._headers,
                json={"title": title, "body": body, "labels": labels},
            )
            # GitHub disables Issues on forks by default and answers 410 Gone.
            # That is a setting, not an outage, so say so instead of surfacing a
            # raw HTTP error several layers up.
            if r.status_code == 410:
                raise IssuesDisabled(
                    f"Issues are disabled on {repo}. Enable them under "
                    "Settings > General > Features > Issues, then re-run."
                )
            if r.status_code == 403:
                raise GitHubPermissionError(
                    f"Token cannot create issues on {repo}. A fine-grained PAT "
                    "needs Issues: read and write."
                )
            r.raise_for_status()
            return int(r.json()["number"])

    async def comment(self, repo: str, issue_number: int, body: str) -> None:
        self._guard(repo)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{API}/repos/{repo}/issues/{issue_number}/comments",
                headers=self._headers,
                json={"body": body},
            )
            r.raise_for_status()

    async def failing_checks(self, repo: str, sha: str) -> list[str]:
        """Names of check runs that concluded in failure for this commit.

        Used to tell a `blocked` PR waiting on a red check (mechanical, worth
        dispatching) from one waiting on a human reviewer (not our business).
        A pending check is not a failure -- it has not finished yet.
        """
        self._guard(repo)
        if not sha:
            return []
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{API}/repos/{repo}/commits/{sha}/check-runs",
                headers=self._headers,
                params={"per_page": 100},
            )
            r.raise_for_status()
            return [
                c.get("name", "?")
                for c in r.json().get("check_runs", [])
                if c.get("conclusion") in FAILING_CONCLUSIONS
            ]


class MockGitHubClient:
    """Serves PRs from a JSON fixture and records writes in memory.

    The fixture holds real stuck PRs captured from apache/superset, so the demo
    exercises genuine titles, ages and merge states rather than invented ones.
    """

    def __init__(self, fixture: str | Path | None = None, world: Any = None) -> None:
        # `world` is shared with the mock agent: a PR it has "fixed" reads back
        # as clean here, so the orchestrator's verification step sees a real
        # change instead of rejecting every success.
        self._world = world
        path = Path(fixture or Path(__file__).parent / "fixtures" / "stuck_prs.json")
        self._prs = {
            p["number"]: _parse_pr(p) for p in json.loads(Path(path).read_text())
        }
        self.failing_by_sha: dict[str, list[str]] = {}
        self.issues: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self._next_issue = 9000

    @property
    def mode(self) -> str:
        return "mock"

    async def list_open_prs(self, repo: str) -> list[PullRequest]:
        return list(self._prs.values())

    async def get_pr(self, repo: str, number: int) -> PullRequest:
        if number not in self._prs:
            raise KeyError(f"no fixture PR #{number}")
        pr = self._prs[number]
        if self._world is not None and number in self._world.resolved:
            # Mirrors the live outcome: the conflict is gone, and CI is now the
            # next thing to settle.
            return PullRequest(
                **{**pr.__dict__, "mergeable_state": "clean", "mergeable": True}
            )
        return pr

    async def create_issue(
        self, repo: str, title: str, body: str, labels: list[str]
    ) -> int:
        self._next_issue += 1
        self.issues.append(
            {
                "number": self._next_issue,
                "title": title,
                "body": body,
                "labels": labels,
                "repo": repo,
            }
        )
        return self._next_issue

    async def comment(self, repo: str, issue_number: int, body: str) -> None:
        self.comments.append({"issue": issue_number, "body": body, "repo": repo})

    async def failing_checks(self, repo: str, sha: str) -> list[str]:
        # Fixture PRs keyed by head_sha; default to "no failures".
        return list(self.failing_by_sha.get(sha, []))


def _parse_pr(d: dict[str, Any]) -> PullRequest:
    user = d.get("user") or {}
    head = d.get("head") or {}
    base = d.get("base") or {}
    return PullRequest(
        number=int(d["number"]),
        title=str(d.get("title", "")),
        author=str(user.get("login", "unknown")),
        created_at=str(d.get("created_at", "1970-01-01T00:00:00Z")),
        updated_at=str(d.get("updated_at", "1970-01-01T00:00:00Z")),
        mergeable_state=str(d.get("mergeable_state", "unknown")),
        draft=bool(d.get("draft", False)),
        mergeable=d.get("mergeable"),
        head_ref=str(head.get("ref", "")),
        base_ref=str(base.get("ref", "master")),
        html_url=str(d.get("html_url", "")),
        head_sha=str(head.get("sha", "")),
    )


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def build_github_client(cfg, world: Any = None) -> GitHubClient:
    if cfg.live_github:
        return LiveGitHubClient(cfg.github_token, cfg)
    return MockGitHubClient(world=world)
