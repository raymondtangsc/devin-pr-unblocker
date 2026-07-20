# Demo deck design — "The Merge Tax" v2

Serves deliverable 2 of the assessment: a ≤5-minute Loom talked over this deck.
Audience: a prospective customer's VP of Engineering + senior ICs, curious about
Devin. **They do not know Superset** — it is the verifiable case study, not the
subject.

## Framing decisions (approved)

- **Problem-led spine**, universal mechanism first: any repo where PRs wait
  while main moves has this physics; Superset makes it measurable and public.
- **Value beat pre-empts the "aren't stuck PRs worthless?" objection** with:
  (1) red is unread, not rejected — reviewers triage green-first, so a dirty PR
  never enters the queue; (2) contributor economics — for a small fix, rebasing
  + diagnosing CI costs more than the patch did, so rational contributors walk;
  (3) the data — 87% of concluded human PRs merge; merge rate decays 91%→64%
  with time stuck. Closing line: "the process never rejects these PRs; it
  starves them — on friction, not merit."
- **Devin is a teammate, not a helper.** Assigned work end-to-end, own
  environment, budget and scope, knows what isn't its job, escalates, reports
  back, gets reviewed. `prompts.py` presented as the onboarding doc.
- **Why-Devin = automated + customizable.** Event-driven, nobody at a keyboard,
  one-HTTP-call integration; and a bespoke flow (own classifier, policies,
  verification) built in hours — the workflow is the product, Devin the engine.
- **Trigger architecture stated and true:** poll for completeness, webhook for
  latency. The scheduled sweep is the source of truth (level-triggered,
  idempotent, zero inbound surface); the webhook is a latency upgrade. The
  poller must exist in code before the deck claims it.

## Beat structure (9 beats, 5:00)

| # | Beat | Window | One claim |
|---|---|---|---|
| 1 | HOOK | 0:00–0:35 | PRs race main; P(stuck) ≈ time × churn × diff. Physics, not process failure |
| 2 | SCALE | 0:35–1:15 | Measured on a verifiable repo: 62% of 376 open PRs blocked on mechanics (145 conflicts, 89 red CI) |
| 3 | VALUE | 1:15–1:55 | The objection, answered ×3; decay bar 91→64; ~39 PRs being lost |
| 4 | SOLUTION | 1:55–2:20 | The teammate + pipeline strip (two trigger inlets, refuse lane: 137 left for humans) |
| 5 | DEMO | 2:20–3:10 | Title card + 4-step strip; screen recording carries it |
| 6 | ARCHITECTURE | 3:10–3:45 | 3 real snippets: deterministic detect / classify-refusal / verify-not-trust; guardrails caption |
| 7 | WHY DEVIN | 3:45–4:15 | Teammate, not helper. Automated + customizable; prompts.py = onboarding doc |
| 8 | PROOF | 4:15–4:40 | 4 metrics; success rate verified against GitHub, not self-report |
| 9 | NEXT | 4:40–5:00 | Read-only week → conflicts-only pilot → CI repair → same rail, more jobs |

## Format

- Single self-contained HTML scroll-deck; each beat `min-height:100vh`, one
  claim per screen, large type.
- **Speaker notes**: per-beat script (~60 words) + time window, hidden by
  default, toggled by the `N` key only (no visible control on recording).
- Thin top progress bar (scroll fraction) to pace against 5:00.
- Palette: validated set (Superset cyan accent; git-state green/amber/red);
  light + dark themes; serif claims / mono receipts.
- Three working visuals only: (1) branch-divergence diagram, (2) decay bars,
  (3) pipeline strip with inlets + refuse lane. Beat 6 shows code snippets.
- Honesty footnotes: replay-PR provenance, closed-PR sample skew, n=5 caveat.
- Published as Artifact **updating the existing URL** (48d2e810…) and committed
  to the repo under `deck/`.

## Build order

1. Poller (`POLL_INTERVAL_SECONDS`, default 600, 0 disables) + test → commit.
2. Deck HTML → publish artifact (same URL) → commit under `deck/`.
