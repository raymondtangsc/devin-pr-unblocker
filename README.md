# Devin PR Unblocker

Finds pull requests blocked on **mechanical** problems — merge conflicts and red CI —
and dispatches a Devin session to fix each one, so reviewer time is spent on
judgement instead of rebases.

It never merges, approves, or closes anything. It moves a PR from *"blocked on
mechanical work"* to *"needs a human decision."*

Why this problem, and every figure behind it: [`notes/findings.md`](notes/findings.md).
The pitch: `open deck/merge-tax.html` (see [`deck/README.md`](deck/README.md)).

---

## Run it

Runs **fully offline in mock mode** — no Devin key, no GitHub token, no network.

### Docker

```bash
git clone https://github.com/raymondtangsc/devin-pr-unblocker && cd devin-pr-unblocker
cp .env.example .env          # works as-is for the mock demo
docker compose up --build

# in a second shell:
bash scripts/demo.sh
open http://localhost:8000/
```

### Local (no Docker)

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env
./.venv/bin/uvicorn app.main:app --port 8000 &
bash scripts/demo.sh
```

### What the demo does

`scripts/demo.sh` walks the whole workflow and prints each step:

1. **A sweep fires** — detection finds every mechanically blocked PR in a fixture of
   22 real stuck PRs captured from `apache/superset`, and files one tracking issue each.
2. **The guardrail refuses** an event aimed at upstream `apache/superset` → `403`.
3. **A maintainer labels three issues** — the human path, which dispatches immediately.
4. **Sessions run and settle**, and the script waits for every one to reach a terminal
   state before printing the metrics.

Expected output — roughly:

```
tracked            16
unblocked           6
needing a human     1
success rate       86%
median unblock     13s
blockers           {'conflict': 8, 'failing_ci': 8}
```

The mock agent deliberately escalates every 4th session. A demo where nothing ever
fails teaches you nothing about how failures surface.

### Poke at it directly

```bash
curl -XPOST localhost:8000/simulate          # run a sweep now
curl -s   localhost:8000/metrics             # JSON metrics
curl -s   localhost:8000/healthz             # mode + target repo
open      http://localhost:8000/             # dashboard, auto-refreshing
```

Fire the human path by hand — this is the one webhook the service accepts:

```bash
curl -XPOST localhost:8000/webhook/github \
  -H 'X-GitHub-Event: issues' -H 'Content-Type: application/json' \
  -d '{"action":"labeled","label":{"name":"devin-unblock"},
       "issue":{"number":900,"title":"Unblock PR #28627: demo"},
       "repository":{"full_name":"raymondtangsc/superset"}}'
```

### Tests

```bash
./.venv/bin/python -m pytest -q
```

---

## Run it for real

No code changes — fill in `.env` and restart:

```bash
DEVIN_API_KEY=cog_...        # service-user key
DEVIN_ORG_ID=org-...         # Settings > Service Users in the Devin dashboard
GITHUB_TOKEN=github_pat_...  # Issues RW, Pull requests RW, Contents RW
GITHUB_REPO=<you>/superset   # your fork -- never upstream
```

`DEVIN_MODE=auto` (the default) goes live as soon as both Devin values are present and
falls back to mock otherwise. `DEVIN_MODE=live` refuses to start without them rather
than silently pretending to dispatch work.

> **Both Devin values are required.** `cog_`-prefixed keys authenticate against the v3
> API, where every route is org-scoped: `/v3/organizations/{org_id}/sessions`. A valid
> key with a missing org id returns `404 Organization not found` — which is how you
> tell it apart from a bad key (`403 Unauthorized`).

### Knobs

| Variable | Default | What it does |
|---|---|---|
| `MAX_DISPATCHES_PER_EVENT` | `3` | Sessions started per sweep. A spend **rate limit**, not a ceiling — the queue drains over later sweeps. `0` = detect only, spend nothing. |
| `POLL_INTERVAL_SECONDS` | `600` | Sweep cadence. `0` = webhook-only. |
| `MIN_QUIET_DAYS` | `3` | Only touch PRs whose author has not pushed for this long. Fractions work (`0.0007` ≈ 1 min, for demos). |
| `DEVIN_MAX_ACU` | `10` | Hard per-session spend ceiling. |
| `SKIP_WHEN_MASTER_RED` | `true` | Don't chase checks already failing on `master`. |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC verification; enforced when set. |

---

## How it works

```
  triggers        scheduled sweep (autonomous)  ·  issues.labeled (human)
       │
       ▼
  detect()        classify open PRs by mergeable_state         deterministic
       │            dirty → conflict    red required check → failing_ci   (no LLM)
       ▼
  record()        file ONE tracking issue per blocked PR        audit trail
       │            labelled `devin-unblock`                    + spend gate
       ▼
  dispatch()      one Devin session per PR, ACU-capped          the agent
       │            prompt: rebase / resolve / fix CI / push
       ▼
  reconcile()     poll to terminal state, VERIFY against GitHub
       │            comment the verdict back on the issue
       ▼
  dashboard       success rate · time-to-unblock · full event log
```

| File | Role |
|---|---|
| `app/config.py` | Config + the upstream-repo guardrail |
| `app/github_client.py` | GitHub REST + fixture-backed mock |
| `app/devin_client.py` | Devin v3 API + deterministic mock |
| `app/prompts.py` | Session prompts (treated as code, not string literals) |
| `app/orchestrator.py` | detect → classify → record → dispatch → reconcile |
| `app/main.py` | FastAPI: webhook, sweep loop, `/metrics`, `/simulate` |
| `app/dashboard.py` | Operator dashboard |
| `app/store.py` | SQLite state machine + metrics |

### The five decisions behind it

1. **Control plane vs. agent plane.** The loop is a deterministic state machine in
   plain code; the agent is confined to the one stage that needs judgement. A failed,
   stuck, or lying session becomes a failed work item, never a wedged system.
2. **Level-triggered, not event-dependent.** Discovery is sweep-only: each cycle
   re-derives the blocked set from repo state, so there are no events to drop. The
   single webhook is the label — the one path where a human is waiting.
3. **Held to the same bar as any teammate.** Hard ACU budget per session; work we
   cannot *prove* is mechanical is never assigned; results count only after GitHub
   confirms them.
4. **Never interrupt a human.** A PR pushed to within `MIN_QUIET_DAYS` is work in
   progress, not rot. The label overrides — a human asking is consent.
5. **Every action leaves a durable record.** The tracking issue is the work item, the
   spend gate, and the audit trail. Our database is *not* the source of truth: before
   filing, `record()` asks GitHub whether an issue already exists and adopts it, so a
   lost volume or a second instance cannot double-file.

---

## Guardrails

- **Upstream is blocked at the lowest level.** `apache/superset` is on a hard
  blocklist; a webhook naming it gets `403`, even if `GITHUB_REPO` were misconfigured.
- **Dispatch is idempotent**, in the store *and* against GitHub.
- **Per-event dispatch cap** and **per-session ACU ceiling**.
- **Devin pushes to the PR branch only** — never `master`, never merges or approves.
  Worst case is one bad branch, recoverable from git history, still behind human review.
- **Drafts are skipped**, and so are checks already red on `master` — no work on a PR
  branch turns those green.

---

## Observability

*"If I were an engineering leader, how would I know this is working?"*

| Metric | Question it answers |
|---|---|
| `succeeded` / `failed` / `success_rate` | Is the agent actually fixing things? |
| `queued` vs `in_flight` | Is the backlog waiting, or is work running? |
| `median_unblock_seconds` | How fast, versus the status quo? |
| `errored` | System faults — **excluded** from the success rate |
| `blocker_mix` | Conflicts vs red CI — where the work really is |

Two definitions worth knowing:

- **"Needs a human" means the agent tried and could not.** Plumbing faults land in a
  separate `errored` state and never touch the success rate — a dispatch that never
  reached Devin says nothing about whether the conflict was resolvable.
- **Success is verified, not reported.** A session claiming success is re-checked
  against GitHub before it counts, so the rate measures results rather than the agent's
  confidence.

---

## Known limitations

- **Pushing to a contributor's fork branch** requires the maintainer-edit flag, which
  not every contributor grants. Real deployments use a maintainer-owned branch or a
  stacked PR. A permissions question, not a technical one.
- **A rescued PR that re-rots is not re-tracked** — terminal work items don't reopen.
  Production needs a re-rot cycle reusing the same issue.
- **ACU consumption is not reported** by the API for these sessions, so no spend
  metric is shown. The per-session ceiling is still enforced.
- **`mergeable_state` is computed lazily** by GitHub and reports `unknown` briefly
  after a push; the client retries, but a sweep immediately after a large merge may
  under-report.
