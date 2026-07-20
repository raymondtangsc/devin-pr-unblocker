"""Operator dashboard.

Answers one question for an engineering leader at a glance: *is this working?*
Summary tiles first, then the per-PR detail, then the raw event log. State is
encoded in shape and text as well as colour, so it survives greyscale and
colour-vision deficiency.
"""

from __future__ import annotations

import html
import time
from typing import Any

from .store import WorkItem

_STATE_STYLE = {
    "detected": ("neutral", "detected"),
    "issue_filed": ("info", "issue filed"),
    "dispatched": ("info", "dispatched"),
    "running": ("warn", "running"),
    "succeeded": ("ok", "unblocked"),
    "failed": ("crit", "needs a human"),
    "skipped": ("neutral", "skipped"),
}

_CSS = """
:root{
  --paper:#FAFBFC;--sunk:#F1F3F6;--card:#fff;--ink:#10141C;--ink-2:#39424F;
  --ink-3:#6B7684;--rule:#DDE2E8;--accent:#20A7C9;--accent-ink:#12708A;
  --ok:#2E9E6B;--warn:#D9902B;--crit:#B02A37;--info:#7C5CD3;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#0D1017;--sunk:#12161F;--card:#161B25;--ink:#EDF0F4;--ink-2:#B3BCC8;
  --ink-3:#7E8794;--rule:#242C38;--accent:#3EB8D6;--accent-ink:#6FD0E6;
  --ok:#33A071;--warn:#C2801F;--crit:#C4444F;--info:#8264DB;}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
     line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:76rem;margin:0 auto;padding:2rem 1.5rem 4rem}
header{display:flex;flex-wrap:wrap;gap:1rem;align-items:baseline;
       justify-content:space-between;margin-bottom:.4rem}
h1{font-size:1.35rem;margin:0;letter-spacing:-.01em}
.sub{color:var(--ink-3);font-size:.83rem;font-family:var(--mono)}
h2{font-size:.75rem;font-family:var(--mono);letter-spacing:.13em;
   text-transform:uppercase;color:var(--ink-3);font-weight:500;
   margin:2.4rem 0 .8rem}
.modes{display:flex;gap:.5rem;flex-wrap:wrap;margin:.9rem 0 0}
.chip{font-family:var(--mono);font-size:.7rem;padding:.22rem .55rem;border-radius:3px;
      border:1px solid var(--rule);color:var(--ink-2);background:var(--card)}
.chip.mock{border-color:var(--warn);color:var(--warn)}
.chip.live{border-color:var(--ok);color:var(--ok)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));
       gap:1px;background:var(--rule);border:1px solid var(--rule);
       border-radius:6px;overflow:hidden;margin-top:1.2rem}
.tile{background:var(--card);padding:1rem 1.1rem}
.tile .n{font-size:1.9rem;line-height:1;font-variant-numeric:tabular-nums;
         letter-spacing:-.02em}
.tile .n.ok{color:var(--ok)}.tile .n.crit{color:var(--crit)}
.tile .l{font-size:.74rem;color:var(--ink-3);margin-top:.45rem;line-height:1.35}
.tbl{overflow-x:auto;border:1px solid var(--rule);border-radius:6px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:.85rem;min-width:52rem}
th,td{padding:.55rem .8rem;text-align:left;border-bottom:1px solid var(--rule);
      vertical-align:top}
th{font-family:var(--mono);font-size:.68rem;letter-spacing:.09em;
   text-transform:uppercase;color:var(--ink-3);font-weight:500;background:var(--sunk)}
tr:last-child td{border-bottom:0}
td.num{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;
       white-space:nowrap}
td.mono{font-family:var(--mono);font-size:.78rem}
.pill{display:inline-flex;align-items:center;gap:.35rem;font-family:var(--mono);
      font-size:.68rem;padding:.18rem .5rem;border-radius:3px;white-space:nowrap}
.pill::before{content:"";width:.45rem;height:.45rem;border-radius:50%;background:currentColor}
.pill.ok{background:color-mix(in srgb,var(--ok) 15%,transparent);color:var(--ok)}
.pill.crit{background:color-mix(in srgb,var(--crit) 15%,transparent);color:var(--crit)}
.pill.warn{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}
.pill.info{background:color-mix(in srgb,var(--info) 15%,transparent);color:var(--info)}
.pill.neutral{background:var(--sunk);color:var(--ink-3)}
.detail{color:var(--ink-2);font-size:.8rem;max-width:30rem}
.muted{color:var(--ink-3);font-size:.76rem}
a.sess{color:var(--accent-ink);text-decoration:none;font-size:.78rem;
       border-bottom:1px solid color-mix(in srgb,var(--accent) 45%,transparent)}
a.sess:hover,a.sess:focus-visible{border-bottom-color:var(--accent)}
a:focus-visible,tr:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.log{font-family:var(--mono);font-size:.75rem;background:var(--card);
     border:1px solid var(--rule);border-radius:6px;overflow-x:auto}
.log div{padding:.32rem .8rem;border-bottom:1px solid var(--rule);white-space:nowrap}
.log div:last-child{border-bottom:0}
.log .t{color:var(--ink-3)}
.empty{padding:2.5rem 1rem;text-align:center;color:var(--ink-3);font-size:.88rem;
       background:var(--card);border:1px solid var(--rule);border-radius:6px}
a{color:var(--accent-ink)}
"""


def render_dashboard(
    metrics: dict[str, Any],
    items: list[WorkItem],
    events: list[dict[str, Any]],
    *,
    modes: dict[str, str],
    repo: str,
    label: str,
) -> str:
    rate = metrics.get("success_rate")
    rate_txt = f"{rate * 100:.0f}%" if rate is not None else "—"
    med = metrics.get("median_unblock_seconds")
    med_txt = _duration(med) if med else "—"

    # ACU consumption is not reported by the API for these sessions, so nothing
    # ACU-shaped is shown: a column of zeroes reads as broken instrumentation.
    # The per-session ceiling is enforced in config, not surfaced as a metric.
    tiles = [
        ("", metrics["total_tracked"], "PRs tracked"),
        ("", metrics["queued"], "queued — awaiting dispatch"),
        ("", metrics["in_flight"], "in flight — session working"),
        ("ok", metrics["succeeded"], "unblocked, verified"),
        ("crit", metrics["failed"], "need a human"),
        ("", rate_txt, "verified success rate"),
        ("", med_txt, "median time to unblock"),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="n {cls}">{html.escape(str(v))}</div>'
        f'<div class="l">{html.escape(lab)}</div></div>'
        for cls, v, lab in tiles
    )

    if items:
        rows = "".join(_row(i) for i in items)
        table = f"""<div class="tbl"><table>
<thead><tr><th>PR</th><th>Title</th><th>Blocker</th>
<th style="text-align:right">Age</th><th>State</th><th>Devin session</th>
<th>Outcome</th></tr></thead>
<tbody>{rows}</tbody></table></div>"""
    else:
        table = (
            '<div class="empty">Nothing tracked yet. Trigger a sweep with '
            "<code>curl -XPOST localhost:8000/simulate</code>.</div>"
        )

    log_html = (
        "".join(
            f'<div><span class="t">{_clock(e["ts"])}</span>  '
            f'{html.escape(str(e["kind"])):<16} {html.escape(str(e["message"]))}</div>'
            for e in events
        )
        or '<div class="t">no events yet</div>'
    )

    mix = metrics.get("blocker_mix") or {}
    mix_txt = " · ".join(f"{k}: {v}" for k, v in sorted(mix.items())) or "—"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PR Unblocker — {html.escape(repo)}</title>
<meta http-equiv="refresh" content="10">
<style>{_CSS}</style></head><body><div class="wrap">
<header>
  <div>
    <h1>PR Unblocker</h1>
    <div class="sub">{html.escape(repo)} · trigger label <code>{html.escape(label)}</code></div>
  </div>
  <div class="sub">auto-refresh 10s · {time.strftime("%H:%M:%S")}</div>
</header>
<div class="modes">
  <span class="chip {modes.get("devin", "")}">devin: {html.escape(modes.get("devin", "?"))}</span>
  <span class="chip {modes.get("github", "")}">github: {html.escape(modes.get("github", "?"))}</span>
  <span class="chip">blockers — {html.escape(mix_txt)}</span>
</div>
<div class="tiles">{tiles_html}</div>
<h2>Work items</h2>
{table}
<h2>Event log</h2>
<div class="log">{log_html}</div>
</div></body></html>"""


def _row(i: WorkItem) -> str:
    cls, label = _STATE_STYLE.get(i.state, ("neutral", i.state))
    detail = html.escape((i.detail or "")[:180])
    # The session link is the thing an engineer actually wants to click, so it
    # gets its own column. "queued" states why a row has none, rather than
    # leaving a blank cell that reads as a bug.
    if i.session_url:
        short = html.escape((i.session_id or "")[:12])
        session = (
            f'<a class="sess" href="{html.escape(i.session_url)}" '
            f'target="_blank" rel="noopener">{short}… &#8599;</a>'
        )
    elif i.state in ("detected", "issue_filed"):
        session = '<span class="muted">queued</span>'
    else:
        session = '<span class="muted">&mdash;</span>'

    return f"""<tr>
<td class="mono">#{i.pr_number}</td>
<td class="detail">{html.escape(i.title[:64])}<br><span class="muted">@{html.escape(i.author)}</span></td>
<td class="mono">{html.escape(i.blocker)}</td>
<td class="num">{i.pr_age_days:.0f}d</td>
<td><span class="pill {cls}">{html.escape(label)}</span></td>
<td class="mono">{session}</td>
<td class="detail">{detail}</td>
</tr>"""


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))
