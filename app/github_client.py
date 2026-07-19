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

# GitHub's mergeable_state values we treat as "mechanically blocked".
#   dirty    -> merge conflicts against the base branch
#   unstable -> mergeable, but a required check is failing
BLOCKED_STATES = {"dirty": "conflict", "unstable": "failing_ci"}


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

    @property
    def age_days(self) -> float:
        created = time.mktime(time.strptime(self.created_at, "%Y-%m-%dT%H:%M:%SZ"))
        return round((time.time() - created) / 86400, 1)

    @property
    def blocker(self) -> str | None:
        """Why this PR cannot merge, or None if it is fine."""
        return BLOCKED_STATES.get(self.mergeable_state)


class GitHubClient(Protocol):
    async def list_open_prs(self, repo: str) -> list[PullRequest]: ...
    async def get_pr(self, repo: str, number: int) -> PullRequest: ...
    async def create_issue(
        self, repo: str, title: str, body: str, labels: list[str]
    ) -> int: ...
    async def comment(self, repo: str, issue_number: int, body: str) -> None: ...
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


class MockGitHubClient:
    """Serves PRs from a JSON fixture and records writes in memory.

    The fixture holds real stuck PRs captured from apache/superset, so the demo
    exercises genuine titles, ages and merge states rather than invented ones.
    """

    def __init__(self, fixture: str | Path | None = None) -> None:
        path = Path(fixture or Path(__file__).parent / "fixtures" / "stuck_prs.json")
        self._prs = {
            p["number"]: _parse_pr(p) for p in json.loads(Path(path).read_text())
        }
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
        return self._prs[number]

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
        head_ref=str(head.get("ref", "")),
        base_ref=str(base.get("ref", "master")),
        html_url=str(d.get("html_url", "")),
    )


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def build_github_client(cfg) -> GitHubClient:
    if cfg.live_github:
        return LiveGitHubClient(cfg.github_token, cfg)
    return MockGitHubClient()
