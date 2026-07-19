"""FastAPI service: webhook receiver, reconciler loop, and observability.

Endpoints
    POST /webhook/github   GitHub events (push, pull_request, issues)
    POST /simulate         fire a synthetic event without GitHub (demo path)
    GET  /metrics          JSON metrics for scraping
    GET  /healthz          liveness
    GET  /                 human-readable dashboard
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import ConfigError, load_config
from .dashboard import render_dashboard
from .devin_client import build_devin_client
from .github_client import build_github_client
from .orchestrator import Orchestrator
from .store import Store

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s"
)
log = logging.getLogger("unblocker.api")

RECONCILE_INTERVAL_SECONDS = 10


def create_app() -> FastAPI:
    cfg = load_config()
    store = Store(cfg.db_path)
    github = build_github_client(cfg)
    devin = build_devin_client(cfg)
    orch = Orchestrator(cfg, store, github, devin)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(reconciler())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            store.close()

    app = FastAPI(title="Devin PR Unblocker", version="1.0.0", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.store = store
    app.state.orch = orch
    app.state.modes = {"github": github.mode, "devin": devin.mode}

    log.info(
        "starting | repo=%s | github=%s | devin=%s | label=%s",
        cfg.github_repo,
        github.mode,
        devin.mode,
        cfg.trigger_label,
    )
    if devin.mode == "mock":
        log.warning(
            "Devin client is in MOCK mode -- set DEVIN_API_KEY and DEVIN_ORG_ID "
            "for live sessions. The pipeline is otherwise identical."
        )

    # ------------------------------------------------------------- lifecycle

    async def reconciler() -> None:
        """Poll in-flight sessions until they settle."""
        while True:
            try:
                settled = await orch.reconcile()
                if settled:
                    log.info("reconciler settled %d item(s)", settled)
            except Exception:
                log.exception("reconciler pass failed")
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)

    # --------------------------------------------------------------- routes

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "modes": app.state.modes, "repo": cfg.github_repo}

    @app.get("/metrics")
    async def metrics() -> dict:
        m = store.metrics()
        m["modes"] = app.state.modes
        m["repo"] = cfg.github_repo
        return m

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return render_dashboard(
            store.metrics(),
            store.all_items(),
            store.recent_events(25),
            modes=app.state.modes,
            repo=cfg.github_repo,
            label=cfg.trigger_label,
        )

    @app.post("/webhook/github")
    async def webhook(
        request: Request,
        x_github_event: str = Header(default=""),
        x_hub_signature_256: str = Header(default=""),
    ) -> JSONResponse:
        raw = await request.body()
        _verify_signature(cfg.github_webhook_secret, raw, x_hub_signature_256)

        payload = await request.json()
        repo = (payload.get("repository") or {}).get("full_name", cfg.github_repo)
        try:
            cfg.assert_repo_allowed(repo)
        except ConfigError as exc:
            # Loud refusal rather than a silent drop: this is the guardrail that
            # keeps the automation off upstream repositories.
            log.error("rejected webhook: %s", exc)
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        result = await _route_event(orch, cfg, x_github_event, payload, repo)
        return JSONResponse(result)

    @app.post("/simulate")
    async def simulate(request: Request) -> JSONResponse:
        """Trigger a full sweep without GitHub, for demos and local runs."""
        body = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        repo = body.get("repo") or cfg.github_repo
        cfg.assert_repo_allowed(repo)
        result = await orch.handle_repo_event(repo, reason="simulated")
        return JSONResponse(result)

    return app


async def _route_event(orch, cfg, event: str, payload: dict, repo: str) -> dict:
    """Map a GitHub event onto a pipeline action."""
    if event == "push":
        ref = payload.get("ref", "")
        if not ref.endswith(("/master", "/main")):
            return {"ignored": f"push to {ref}"}
        # Every merge into master can conflict open PRs -- this is the event that
        # actually manufactures the backlog, so it drives the sweep.
        return await orch.handle_repo_event(repo, reason=f"push to {ref}")

    if event == "pull_request":
        action = payload.get("action")
        if action not in {"opened", "reopened", "synchronize"}:
            return {"ignored": f"pull_request.{action}"}
        number = (payload.get("pull_request") or {}).get("number")
        if number is None:
            return {"ignored": "pull_request without a number"}
        pr = await orch.github.get_pr(repo, number)
        if pr.blocker is None or pr.draft:
            return {"pr": number, "blocked": False}
        await orch.record(repo, pr, pr.blocker)
        return {"pr": number, "blocked": True, "blocker": pr.blocker, "tracked": True}

    if event == "issues":
        action = payload.get("action")
        if action != "labeled":
            return {"ignored": f"issues.{action}"}
        label = (payload.get("label") or {}).get("name", "")
        if label != cfg.trigger_label:
            return {"ignored": f"label {label!r}"}
        issue = payload.get("issue") or {}
        pr_number = _pr_number_from_issue(issue)
        if pr_number is None:
            return {"ignored": "labelled issue does not reference a PR"}
        item = await orch.dispatch(repo, pr_number)
        return {
            "pr": pr_number,
            "dispatched": bool(item and item.session_id),
            "session": item.session_id if item else None,
        }

    return {"ignored": f"event {event!r}"}


def _pr_number_from_issue(issue: dict) -> int | None:
    """Recover the PR number a tracking issue refers to.

    The title carries it in a fixed shape ("Unblock PR #1234: ..."), which keeps
    the label trigger working even if the issue body is edited.
    """
    import re

    for field in (issue.get("title", ""), issue.get("body", "")):
        m = re.search(r"PR #(\d+)", field or "")
        if m:
            return int(m.group(1))
    return None


def _verify_signature(secret: str, body: bytes, header: str) -> None:
    """Validate GitHub's HMAC signature.

    When no secret is configured the check is skipped so the demo runs without
    one -- but if a secret IS set, a missing or wrong signature is rejected.
    """
    if not secret:
        return
    if not header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="missing signature")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        raise HTTPException(status_code=401, detail="bad signature")


app = create_app()
