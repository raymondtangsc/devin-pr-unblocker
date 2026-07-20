# The deck

`merge-tax.html` is a self-contained talk deck for the ≤5-minute Loom. No build,
no server, no network — open the file.

```bash
open deck/merge-tax.html          # macOS
xdg-open deck/merge-tax.html      # Linux
```

## Presenting

| Key | Does |
|---|---|
| **N** | Toggle speaker notes — each carries its time window and a ~60-word script |
| **T** | Pin light/dark (defaults to your OS; pin it so recording matches what you rehearsed) |
| scroll | One beat per screen; the thin bar at the top tracks pace against 5:00 |

Both buttons sit at zero opacity until hovered, so neither appears in a recording.
`merge-tax.html#notes` opens with notes already on.

Rehearse with notes on, record with them off. The scripts are written to their
windows, so if reading one aloud comfortably overruns, the beat is too long.

## Structure

```
0  0:00  title            PR rot, in one sentence
1  0:15  what is PR rot   the mechanism, the merge tax, the double loss
2  0:50  scale            153 of 376 open PRs, and every other blocker sized
3  1:25  cost             rework toll · "weren't they bad PRs?" · attrition
4  2:00  DEMO             a PR approved 286 days ago, unblocked and verified
5  2:45  what it does     pipeline + which slice of the backlog it takes
6  3:20  architecture     the five decisions
7  3:55  why Devin        a teammate, not a helper
8  4:20  proof            how you would know it is working
9  4:45  next             the two-week rollout
```

At beat 4 the deck deliberately goes quiet — cut to the screen recording
(dashboard, the Devin session, the PR going green), then come back for beat 5.

Every figure traces to [`../notes/findings.md`](../notes/findings.md).
