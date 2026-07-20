# Research notes — apache/superset

Every figure verified against `git log` or the GitHub REST API with an
authenticated token (5,000 req/hr). Dated 2026-07-20. Numbers marked
**[CORRECTED]** replace an earlier figure that came from a bad sample; the wrong
version must not reappear in the deck.

---

## 1. What kind of repo is this

| Metric | Value |
|---|---|
| Commits 2024 / 2025 / 2026-to-July | 2,068 / 1,996 / **3,121** (~5,350 annualised) |
| Distinct authors, trailing 12 mo | **349** |
| Commits, trailing 12 mo | 4,213 |
| Merge velocity | ~7 commits/day into `master` |

Work mix (trailing 12 months): `chore` **41%**, `fix` **39%**, `feat` **10%**.
Of the chore bucket, `deps` + `deps-dev` = **1,550 commits = 37% of everything**.
Largest single contributor is **dependabot at 1,486 commits (35%)**, more than
the top five humans combined.

Verdict: **mature and accelerating, not maintenance mode.** Abundant
contribution, scarce maintainer attention.

---

## 2. The PR backlog — full population, n=376

**[CORRECTED]** An earlier 22-PR sample claimed "86% of the oldest cohort is
mechanically blocked" and put `unstable` at ~41%. Both were wrong: the sample was
small *and* did not retry GitHub's lazily-computed `mergeable_state`, so most
values were stale.

True distribution across **all 376 open PRs** (after retrying `unknown`):

| State | n | % | Cause | Mechanical? |
|---|---|---|---|---|
| `blocked` | 219 | **58.2%** | Branch protection unsatisfied — missing review, unresolved threads, CODEOWNERS, **or** a required check red | Mostly **human** |
| `dirty` | 145 | **38.6%** | Merge conflicts | **Always mechanical** |
| `unstable` | 7 | 1.9% | Non-required check failing | Mechanical |
| `clean` | 5 | 1.3% | Ready | — |
| `behind` | 0 | 0% | Would need "require up-to-date branches" (not enabled) | Mechanical |

### Resolving the `blocked` bucket (inspecting check runs, n=219)

`mergeable_state` alone is not enough. Fetching check runs for every `blocked`
PR splits it cleanly:

| | n | % of blocked |
|---|---|---|
| a required check is RED -> mechanical, dispatch | **82** | 37% |
| nothing red -> awaiting human review, SKIP | **137** | 62% |
| unreadable -> skip (fail safe) | 0 | — |

Most common failing checks: `pre-commit (current)` 23, `changes` 21,
`playwright-tests-required` 17, `test-postgres-required` 12,
`unit-tests-required` 11, `test-sqlite` 10.

### **[CORRECTED]** Total addressable

    dirty 145  +  unstable 7  +  blocked-with-red-check 82  =  234
    = 62% of all 376 open PRs are mechanically blocked
    = 137 (36%) genuinely await human review -> out of scope by design

### **[CORRECTED]** Blocked vs actionable

62% (234) are mechanically blocked, but the teammate does not touch all of them:

    234 blocked
    - 66 drafts (author still working)
    - 15 outstanding CHANGES_REQUESTED
    = 153 ACTIONABLE  (40% of all open PRs) -- 93 conflicts, 60 red CI

Quote **62%** for the size of the problem and **153 / 40%** for what the system
would work. Do not say "145 conflicts assigned" -- 43 of those are drafts and
some carry change requests; the conflict lane is **93**.

**Headline claim for the deck:** *62% of open PRs are blocked on mechanical
work; 153 are actionable today; 36% await a human reviewer and are deliberately
untouched.*
Do **not** say 86% (bad sample) or 39% (missed the blocked bucket).

**Also corrected:** an earlier note called the CI path "dead weight at 1.9%".
Wrong — CI-blocked PRs mostly appear as `blocked`, not `unstable`. The true
split is **145 conflicts / 89 failing-CI**, so both prompt paths earn their
place.

### Conflict rate rises sharply with age

| Age | n | dirty | % dirty |
|---|---|---|---|
| 0–30d | 139 | 23 | **17%** |
| 30–90d | 75 | 38 | **51%** |
| 90–365d | 143 | 76 | 53% |
| >1y | 19 | 8 | 42% |

Conflict rate **triples** between month 1 and month 2.
Driver ≈ `time-open × base-churn × diff-surface`.

---

## 3. Are these PRs worth merging? (the load-bearing question)

Outcome of **human-authored** closed PRs, by how long they stayed open (n=550,
from the 800 most recently updated closed PRs):

| Lifespan | n | merged | abandoned | merge rate |
|---|---|---|---|---|
| <1 week | 321 | 293 | 28 | **91%** |
| 1–4 weeks | 116 | 105 | 11 | 90% |
| 1–3 months | 57 | 49 | 8 | 85% |
| 3–12 months | 51 | 33 | 18 | **64%** |
| >1 year | 5 | 3 | 2 | 60% *(n too small to cite)* |

**Overall human merge rate: 483/550 = 87%.**

So merging is the *default* outcome — contributors do not simply walk away, and
maintainers do want this work. What kills a PR is **time spent stuck**: merge
probability falls 91% → 64% as it ages.

Engagement on the 145 conflicted PRs specifically:

- **56%** have at least one **human** review (465 human vs 471 bot reviews —
  always separate these; `codeant-ai`, `bito-code-review`, `copilot-*` and
  `korbit-ai` account for most raw review counts)
- 13% were APPROVED at some point
- 17% never reviewed by anyone
- Authors: 52% CONTRIBUTOR, 28% NONE (drive-by), 19% MEMBER

**Value estimate:** 145 conflicted PRs × ~27-point merge-rate gap ≈ **39 PRs of
finished, wanted, already-reviewed work on track to be lost.**

### Caveats to state out loud
- Sample is the 800 most recently *updated* closed PRs → under-represents
  long-dead ones, so true abandonment is likely **worse**, not better.
- The `>1 year` row is n=5. Do not cite it.
- Correlation, not proof: PRs that sit may differ in quality from those that
  merge fast. The claim is that time-stuck is *a* cause, supported by the
  mechanism (conflicts triple with age), not that it is the only one.

---

## 4. Issues — mostly NOT confirmed bugs

267 open issues (an earlier count of 164 was a pagination truncation):

| Category | n | % |
|---|---|---|
| labelled `#bug:*` | 31 | 11% |
| `validation:required` (unreproduced) | 50 | **18%** — largest single label |
| proposals (`sip` / `design:proposal`) | 38 | 14% |

Largest label is *awaiting validation*. An "auto-fix the issue backlog" use case
would be weak: most items need triage and repro first.

**PR↔issue linkage:** only **21%** of PRs formally link an issue
(`Fixes/Closes #N`); **54% link nothing**. So tracking issues we create are new
artifacts, not duplicates — no collision with upstream practice.

---

## 5. Use cases considered and rejected

| Candidate | Verdict | Why |
|---|---|---|
| Automate dependency upgrades | **Killed** | Already solved — dependabot PRs merge in a median of **0.21 d** (5 h), p90 0.7 d, 0% stall |
| Sweep a security bug class | **Killed** | Real (unescaped SQL `LIKE`, 17 live sites, 3 upstream hand-fixes) but a **one-shot** |
| Bug-class regression guard | **Killed** | A CI lint rule is cheaper and deterministic for *new* violations |
| **Unblock the PR backlog** | **Kept** | 145 conflicted PRs, recurring by arithmetic, immune to scripting, invisible to CI |

Time-to-merge, the two-lane contrast that anchors the pitch:

| Cohort | median | p90 | >30 d |
|---|---|---|---|
| dependabot | **0.21 d** | 0.7 d | 0% |
| human-authored | **3.34 d** | **55.4 d** | 18% |

Dependabot is not reviewed more leniently — nothing *mechanical* ever blocks it.
Superset already ran that experiment 1,486 times.

---

## 6. Trigger design — IMPLEMENTED

Shipped in `Orchestrator.classify`. The rule:

```
dirty                        -> dispatch (always mechanical)
behind                       -> dispatch (trivial rebase)
unstable                     -> dispatch (rare but real)
blocked + required check red -> dispatch (mechanical, hidden in the big bucket)
blocked + awaiting review    -> SKIP  (this is the human's job)
```

That last line keeps the pitch honest: 58% of the backlog waits on reviewer
attention and Devin does not touch it. Devin guarantees that *when* a reviewer
looks, the PR is actually mergeable.
