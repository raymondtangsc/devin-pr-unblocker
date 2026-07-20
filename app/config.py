"""Configuration and safety guardrails.

The single most important thing in this module is ``assert_repo_allowed``.
This automation files issues and dispatches agents that push commits; pointed at
the wrong repository it would spam a real open-source project. Upstream
``apache/superset`` is therefore blocked at the lowest level rather than merely
being "not the default".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

# Repositories this system must never write to, no matter how it is configured.
BLOCKED_REPOS = frozenset(
    {
        "apache/superset",
        "apache/incubator-superset",
    }
)


class ConfigError(RuntimeError):
    """Raised when configuration is missing or unsafe."""


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    devin_api_key: str = field(default_factory=lambda: _env("DEVIN_API_KEY"))
    devin_org_id: str = field(default_factory=lambda: _env("DEVIN_ORG_ID"))
    devin_api_base: str = field(
        default_factory=lambda: _env("DEVIN_API_BASE", "https://api.devin.ai/v3")
    )
    devin_mode: str = field(default_factory=lambda: _env("DEVIN_MODE", "auto"))
    devin_max_acu: int = field(default_factory=lambda: _env_int("DEVIN_MAX_ACU", 10))

    github_token: str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    github_repo: str = field(
        default_factory=lambda: _env("GITHUB_REPO", "raymondtangsc/superset")
    )
    github_webhook_secret: str = field(
        default_factory=lambda: _env("GITHUB_WEBHOOK_SECRET")
    )

    trigger_label: str = field(
        default_factory=lambda: _env("TRIGGER_LABEL", "devin-unblock")
    )
    max_dispatches_per_event: int = field(
        default_factory=lambda: _env_int("MAX_DISPATCHES_PER_EVENT", 3)
    )
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "data/unblocker.db"))
    # The scheduled sweep is the source of truth for detection: webhooks are a
    # latency optimization that can be dropped (deploy windows, dead tunnels,
    # exhausted retries), and a silently missed PR is the worst failure mode for
    # a system whose whole point is that nothing falls through the cracks.
    # Sweeps are idempotent, so re-detection is free. 0 disables (webhook-only).
    poll_interval_seconds: int = field(
        default_factory=lambda: _env_int("POLL_INTERVAL_SECONDS", 600)
    )

    @property
    def live_devin(self) -> bool:
        """Whether to call the real Devin API.

        ``auto`` degrades to mock when credentials are absent, so the demo runs
        end-to-end on a laptop with no keys. ``live`` is explicit and fails loudly
        rather than silently pretending to dispatch work.
        """
        if self.devin_mode == "mock":
            return False
        if self.devin_mode == "live":
            if not (self.devin_api_key and self.devin_org_id):
                raise ConfigError(
                    "DEVIN_MODE=live requires both DEVIN_API_KEY and DEVIN_ORG_ID. "
                    "Find the org id under Settings > Service Users."
                )
            return True
        return bool(self.devin_api_key and self.devin_org_id)

    @property
    def live_github(self) -> bool:
        return bool(self.github_token)

    def assert_repo_allowed(self, repo: str) -> None:
        """Refuse to act on a repository outside the configured fork.

        Two independent checks: the repo must match the configured target *and*
        must not be on the upstream blocklist. Either alone would be defeated by
        a careless ``GITHUB_REPO`` edit.
        """
        normalized = repo.strip().lower()
        if normalized in BLOCKED_REPOS:
            raise ConfigError(
                f"Refusing to operate on upstream repository {repo!r}. "
                "This automation may only touch your own fork."
            )
        if normalized != self.github_repo.strip().lower():
            raise ConfigError(
                f"Repository {repo!r} does not match configured GITHUB_REPO "
                f"{self.github_repo!r}; refusing to act."
            )


def load_config() -> Config:
    cfg = Config()
    # Fail at import time rather than at the first write attempt.
    if cfg.github_repo.strip().lower() in BLOCKED_REPOS:
        raise ConfigError(
            f"GITHUB_REPO is set to upstream {cfg.github_repo!r}. Point it at your fork."
        )
    return cfg
