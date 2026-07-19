"""Devin API client (v3) with a faithful mock fallback.

The real client targets ``POST /v3/organizations/{org_id}/sessions``, which is
what ``cog_``-prefixed service-user keys authenticate against. The mock
implements the same interface and the same status vocabulary so the whole
pipeline — dispatch, poll, terminal state, ACU accounting — can be exercised
without credentials or spend.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

# Devin session statuses we treat as finished. The API has historically used
# several spellings; normalise rather than assume one.
_SUCCESS = {"finished", "completed", "succeeded", "blocked_finished"}
_FAILURE = {"expired", "failed", "cancelled", "canceled", "terminated"}

# "blocked" means the agent stopped to ask a human something. For an unattended
# pipeline that is an outcome, not a waiting room -- nobody is watching the
# session to answer. Treat it as terminal and escalate, otherwise these items
# poll forever and quietly inflate the in-flight count.
_NEEDS_HUMAN = {"blocked", "suspended"}


@dataclass
class Session:
    session_id: str
    url: str
    status: str
    acus_consumed: float = 0.0
    structured_output: dict[str, Any] | None = None
    pull_requests: list[dict[str, Any]] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status.lower() in _SUCCESS | _FAILURE | _NEEDS_HUMAN

    @property
    def is_success(self) -> bool:
        return self.status.lower() in _SUCCESS

    @property
    def needs_human(self) -> bool:
        return self.status.lower() in _NEEDS_HUMAN


class DevinClient(Protocol):
    async def create_session(
        self, prompt: str, *, title: str, tags: list[str], max_acu: int
    ) -> Session: ...

    async def get_session(self, session_id: str) -> Session: ...

    @property
    def mode(self) -> str: ...


class LiveDevinClient:
    """Talks to the real Devin v3 API."""

    def __init__(self, api_key: str, org_id: str, base_url: str) -> None:
        if not api_key or not org_id:
            raise ValueError("LiveDevinClient requires both api_key and org_id")
        self._key = api_key
        self._org = org_id
        self._base = base_url.rstrip("/")

    @property
    def mode(self) -> str:
        return "live"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    async def create_session(
        self, prompt: str, *, title: str, tags: list[str], max_acu: int
    ) -> Session:
        body = {
            "prompt": prompt,
            "title": title,
            "tags": tags,
            # A hard ceiling per session. Without this a pathological merge
            # conflict could consume unbounded ACUs before anyone notices.
            "max_acu_limit": max_acu,
            "structured_output_required": True,
            "structured_output_schema": OUTCOME_SCHEMA,
        }
        url = f"{self._base}/organizations/{self._org}/sessions"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=self._headers, json=body)
            resp.raise_for_status()
            return _parse_session(resp.json())

    async def get_session(self, session_id: str) -> Session:
        url = f"{self._base}/organizations/{self._org}/sessions/{session_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=self._headers)
            resp.raise_for_status()
            return _parse_session(resp.json())


class MockDevinClient:
    """Deterministic-ish stand-in used when credentials are absent.

    Sessions progress through ``running`` for a few polls and then land on a
    terminal status. The failure rate is deliberately non-zero: a demo where
    everything succeeds teaches an engineering audience nothing about how the
    system reports problems.
    """

    def __init__(self, *, fail_every: int = 4, polls_to_finish: int = 2) -> None:
        self._counter = itertools.count(1)
        self._sessions: dict[str, dict[str, Any]] = {}
        # Deterministic rather than probabilistic: a demo must show the failure
        # path every time, not "usually". Every Nth session is escalated.
        self._fail_every = fail_every
        self._polls_to_finish = polls_to_finish
        self._rng = random.Random(1337)  # only for cosmetic ACU/file counts

    @property
    def mode(self) -> str:
        return "mock"

    async def create_session(
        self, prompt: str, *, title: str, tags: list[str], max_acu: int
    ) -> Session:
        await asyncio.sleep(0.05)
        index = next(self._counter)
        sid = f"mock-session-{index:04d}"
        self._sessions[sid] = {
            "polls": 0,
            "doomed": index % self._fail_every == 0,
            "created": time.time(),
            "max_acu": max_acu,
        }
        return Session(
            session_id=sid,
            url=f"https://app.devin.ai/sessions/{sid}",
            status="running",
        )

    async def get_session(self, session_id: str) -> Session:
        await asyncio.sleep(0.02)
        state = self._sessions.get(session_id)
        if state is None:
            raise KeyError(f"unknown mock session {session_id}")
        state["polls"] += 1
        url = f"https://app.devin.ai/sessions/{session_id}"

        if state["polls"] < self._polls_to_finish:
            return Session(session_id, url, "running", acus_consumed=0.4 * state["polls"])

        if state["doomed"]:
            return Session(
                session_id,
                url,
                "blocked",
                acus_consumed=round(self._rng.uniform(1.0, 3.0), 2),
                structured_output={
                    "outcome": "failed",
                    "summary": "Conflict in a generated lockfile could not be resolved "
                    "without regenerating it; escalating to a human.",
                },
            )

        acus = round(self._rng.uniform(1.5, 6.0), 2)
        return Session(
            session_id,
            url,
            "finished",
            acus_consumed=acus,
            structured_output={
                "outcome": "succeeded",
                "summary": "Rebased onto master, resolved conflicts preserving the "
                "contributor's intent, fixed the failing lint job.",
                "files_changed": self._rng.randint(1, 9),
            },
            pull_requests=[{"url": "https://github.com/example/pr"}],
        )


# Schema the agent must fill in. Forcing structured output means the orchestrator
# can classify the outcome without parsing prose.
OUTCOME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["succeeded", "failed", "not_needed"],
            "description": "succeeded = branch now rebased and CI-clean; "
            "failed = needs a human; not_needed = PR was not actually blocked.",
        },
        "summary": {
            "type": "string",
            "description": "One or two sentences a reviewer can read at a glance.",
        },
        "conflicts_resolved": {"type": "integer"},
        "files_changed": {"type": "integer"},
    },
    "required": ["outcome", "summary"],
}


def _parse_session(payload: dict[str, Any]) -> Session:
    """Normalise a v3 session payload.

    The API reports status under ``status`` and, in some responses, refines it
    with ``status_detail``; prefer the coarse field and keep the detail for the
    dashboard.
    """
    status = str(payload.get("status") or payload.get("status_enum") or "unknown")
    return Session(
        session_id=str(payload.get("session_id", "")),
        url=str(payload.get("url", "")),
        status=status,
        acus_consumed=float(payload.get("acus_consumed") or 0),
        structured_output=payload.get("structured_output"),
        pull_requests=payload.get("pull_requests"),
    )


def build_devin_client(cfg) -> DevinClient:
    """Pick a client based on config, degrading to mock without credentials."""
    if cfg.live_devin:
        return LiveDevinClient(cfg.devin_api_key, cfg.devin_org_id, cfg.devin_api_base)
    return MockDevinClient()
