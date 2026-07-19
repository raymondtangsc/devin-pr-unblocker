# Devin PR Unblocker

An event-driven automation that finds pull requests blocked on **mechanical**
problems — merge conflicts and red CI — and dispatches a Devin session to fix
each one, so maintainer review time is spent on judgement instead of rebases.

It never merges, approves, or closes anything. It moves a PR from *"blocked on
mechanical work"* to *"needs a human decision."*

---

## Why this problem

Built after profiling `apache/superset`: 8,000 commits, 376 open PRs, 269 open
issues. Every figure below is reproducible from `git log` and the public GitHub
REST API.

| Finding | Value |
|---|---|
| Open PRs | **376**, median age **71 days**, 43% older than 90 days |
| Authors holding those PRs | 192 — and **151 have exactly one** (drive-by contributors) |
| Oldest PRs mechanically blocked | **86%** (10 conflicted, 9 red CI, 3 clean of 22 sampled) |
| Newest PRs mechanically blocked | **42%** — mostly red CI, not yet conflicted |
| Time to merge, dependabot | median **0.21 d** (5 hours), p90 0.7 d |
| Time to merge, human-authored | median **3.34 d**, p90 **55.4 d**, 18% over a month |

The two-lane split is the whole argument. Dependabot's PRs are not reviewed more
leniently — they merge in five hours because **nothing mechanical ever blocks
them**: conflict → auto-rebase, red CI → regenerate. Superset already ran this
experiment 1,486 times. Remove mechanical friction and merge time collapses ~16×.

PRs don't get rejected here. They **rot**: a PR waits for review, `master` moves
underneath it (~7 commits/day), it goes `dirty`, and now only the original author
can fix it — who, months later, is gone. Every merge into `master` manufactures
more of this, which is why it is recurring work rather than a one-off cleanup.

### Why not a script, and why not CI?

- **Detection** *is* a script here — an API query and a dictionary lookup, no
  model involved. Spending an agent on detection would be silly.
- **Resolution** is not. `git rebase` takes you to the conflict and stops.
  Deciding what happens when a contributor rewrote a function that `master`
  refactored underneath them requires understanding both intents.
- **CI reports red. It cannot turn it green.** And CI is the wrong instrument
  for the already-conflicted PRs: they don't fail a check, they simply cannot be
  merged.

---

## Architecture

```
  GitHub event                  push to master · pull_request · issues.labeled
       │
       ▼
  detect()        classify open PRs by mergeable_state          deterministic
       │            dirty → conflict     unstable → failing_ci   (no LLM)
       ▼
  record()        file ONE tracking issue per blocked PR         audit trail
       │            labelled `devin-unblock`                     + approval gate
       ▼
  dispatch()      one Devin session per PR, ACU-capped           the agent
       │            prompt: rebase / resolve / fix CI / push
       ▼
  reconcile()     poll sessions → terminal state                 background loop
       │            comment the verdict back on the issue
       ▼
  dashboard       success rate · time-to-unblock · ACUs/success
```

Why an issue in the middle: the trigger is repository activity, but the issue is
the durable work item, the audit record, and the spend gate. A maintainer can
also apply the label by hand to point Devin at any PR.

| File | Role |
|---|---|
| `app/config.py` | Config + the upstream-repo guardrail |
| `app/github_client.py` | GitHub REST + fixture-backed mock |
| `app/devin_client.py` | Devin v3 API + deterministic mock |
| `app/prompts.py` | Session prompts (treated as code, not string literals) |
| `app/orchestrator.py` | detect → record → dispatch → reconcile |
| `app/main.py` | FastAPI: webhook, `/metrics`, `/simulate` |
| `app/dashboard.py` | Operator dashboard |
| `app/store.py` | SQLite state machine + metrics |

---

## Quick start

Runs **fully offline in mock mode** — no Devin key, no GitHub token, no network.

### Docker

```bash
cp .env.example .env          # works as-is for the mock demo
docker compose up --build
# in another shell:
bash scripts/demo.sh
open http://localhost:8000/
```

### Local

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env
./.venv/bin/uvicorn app.main:app --port 8000 &
bash scripts/demo.sh
```

`scripts/demo.sh` fires a simulated push, shows the guardrail rejecting an event
aimed at upstream, labels three tracking issues, then waits for every session to
reach a terminal state and prints the metrics.

---

## Going live

Mock mode exists so the pipeline is demonstrable without spend. To run for real,
fill in `.env`:

```bash
DEVIN_API_KEY=cog_...        # service-user key
DEVIN_ORG_ID=...             # Settings > Service Users in the Devin dashboard
GITHUB_TOKEN=ghp_...         # repo scope
GITHUB_REPO=<you>/superset   # your fork -- never upstream
GITHUB_WEBHOOK_SECRET=...    # optional; enforced when set
```

No code changes. `DEVIN_MODE=auto` (the default) uses the live API as soon as
both `DEVIN_API_KEY` and `DEVIN_ORG_ID` are present, and falls back to mock
otherwise. `DEVIN_MODE=live` refuses to start without them rather than silently
pretending to dispatch work.

> **Both values are required.** `cog_`-prefixed keys authenticate against the v3
> API, where every route is org-scoped: `/v3/organizations/{org_id}/sessions`.
> A valid key with a missing or wrong org id returns `404 Organization not
> found` — which is how you tell it apart from a bad key (`403 Unauthorized`).

To receive real webhooks, expose the service and point a GitHub webhook at
`POST /webhook/github` for the **push**, **pull_request**, and **issues** events:

```bash
ngrok http 8000     # then use https://<id>.ngrok.app/webhook/github
```

---

## Guardrails

Spending money and pushing commits on someone's behalf deserves more than a
sensible default.

- **Upstream is blocked at the lowest level.** `apache/superset` is on a hard
  blocklist; a webhook naming it gets `403` with an explicit refusal, even if
  `GITHUB_REPO` were misconfigured. Both checks must pass — matching the
  configured repo *and* not being on the blocklist.
- **Dispatch is idempotent.** Re-detection and re-labelling never open a second
  session for the same PR. Detection runs on every push, so without this the
  first busy day would bill the same work repeatedly.
- **Per-event dispatch cap** (`MAX_DISPATCHES_PER_EVENT`, default 3). The first
  run against a 376-PR backlog would otherwise open hundreds of sessions. The
  surplus is queued and logged, never silently dropped.
- **Per-session ACU ceiling** (`DEVIN_MAX_ACU`).
- **Devin pushes to the PR branch only.** Never to `master`; never merges,
  approves, or closes.
- **Drafts are skipped** — the author is still working.

---

## Observability

*"If I were an engineering leader, how would I know this is working?"*

`GET /metrics` (JSON) and `/` (dashboard, auto-refreshing):

| Metric | Question it answers |
|---|---|
| `succeeded` / `failed` / `success_rate` | Is the agent actually fixing things? |
| `median_unblock_seconds` | How fast, versus the 71-day status quo? |
| `acus_per_success` | Unit economics — cost per rescued PR |
| `in_flight`, `by_state` | What's happening right now |
| `blocker_mix` | Conflicts vs red CI — where the work really is |

Success rate is measured over attempts that reached a *verdict*, so queued work
doesn't dilute it into meaninglessness early on. Every state transition is
timestamped in SQLite, so the event log is a genuine audit trail.

The mock deliberately escalates every 4th session. A demo where everything
succeeds teaches an engineering audience nothing about how failures surface.

---

## Tests

```bash
./.venv/bin/python -m pytest -q     # 24 tests
```

Coverage is concentrated on what would cause real damage: the upstream
guardrail, dispatch idempotency (duplicate sessions cost money), the
per-event cap, and outcome classification.

One case worth calling out: Devin's `blocked` status means the agent stopped to
ask a human something. In an unattended pipeline nobody is there to answer, so
`blocked` is treated as **terminal and escalated** rather than polled forever —
otherwise those items inflate the in-flight count indefinitely.

---

## Known limitations

- **Pushing to a contributor's fork branch** requires the maintainer-edit flag,
  which not every contributor grants. In a real engagement the pattern is a
  maintainer-owned branch or a stacked PR. This is a permissions question, not a
  technical one, and worth settling early.
- **`mergeable_state` is computed lazily** by GitHub and reports `unknown`
  briefly after a push; the client retries, but a sweep immediately after a
  large merge may under-report.
- **The reconciler polls** on a fixed interval. Fine at this scale; a real
  deployment would take Devin webhooks instead.
- **The PR fixture is a 22-PR sample** captured from upstream, matching the real
  distribution (10 `dirty` / 9 `unstable` / 3 `clean`). Profiling all 376
  requires an authenticated token.
