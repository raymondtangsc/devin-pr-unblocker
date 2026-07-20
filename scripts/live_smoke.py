#!/usr/bin/env python
"""Live smoke test against the real Devin API.

Proves the integration works through the application's own client -- create a
session, poll it, parse the payload -- rather than through curl. Deliberately
cheap: a tiny prompt and a low ACU ceiling.

    ./.venv/bin/python scripts/live_smoke.py            # read-only checks
    ./.venv/bin/python scripts/live_smoke.py --create   # actually spend ACUs
"""

from __future__ import annotations

import asyncio
import sys

import httpx

sys.path.insert(0, ".")

from app.config import load_config  # noqa: E402
from app.devin_client import LiveDevinClient, _parse_session  # noqa: E402


async def main() -> int:
    cfg = load_config()
    if not (cfg.devin_api_key and cfg.devin_org_id):
        print("DEVIN_API_KEY / DEVIN_ORG_ID not set in .env")
        return 2

    client = LiveDevinClient(cfg.devin_api_key, cfg.devin_org_id, cfg.devin_api_base)
    base = f"{cfg.devin_api_base}/organizations/{cfg.devin_org_id}"
    print(f"org  {cfg.devin_org_id}\nbase {cfg.devin_api_base}\n")

    # 1. Auth + list, and confirm the parser handles real payloads.
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(
            f"{base}/sessions?limit=10", headers={"Authorization": f"Bearer {cfg.devin_api_key}"}
        )
        r.raise_for_status()
        items = r.json().get("items", [])

    print(f"[ok] auth + list: {len(items)} session(s)")
    for raw in items[:5]:
        s = _parse_session(raw)
        flags = []
        if s.is_terminal:
            flags.append("terminal")
        if s.needs_human:
            flags.append("needs-human")
        print(
            f"     {s.status:<11} {s.status_detail or '-':<12} acu={s.acus_consumed:<5} "
            f"{'/'.join(flags) or 'in-flight':<22} {raw.get('title', '')[:38]}"
        )

    # 2. Single-session fetch through the client itself.
    if items:
        sid = items[0]["session_id"]
        s = await client.get_session(sid)
        assert s.session_id == sid, "client returned the wrong session"
        print(f"\n[ok] client.get_session({sid[:12]}...) -> {s.status}")

    if "--create" not in sys.argv:
        print("\nRead-only checks passed. Re-run with --create to spend ACUs.")
        return 0

    # 3. Create a real session.
    print("\ncreating a live session (ACU ceiling 1)...")
    created = await client.create_session(
        "Reply with the single word ACK. Do not clone any repository, do not "
        "write any files, and do not open a pull request. This is a connectivity "
        "smoke test for an API integration.",
        title="pr-unblocker connectivity smoke test",
        tags=["pr-unblocker", "smoke-test"],
        max_acu=1,
    )
    print(f"[ok] created {created.session_id}\n     {created.url}\n     status={created.status}")

    for attempt in range(10):
        await asyncio.sleep(6)
        s = await client.get_session(created.session_id)
        print(
            f"     poll {attempt + 1}: status={s.status} detail={s.status_detail or '-'} "
            f"acu={s.acus_consumed} terminal={s.is_terminal}"
        )
        if s.is_terminal:
            print(f"\n[ok] reached terminal state: {s.status}")
            if s.structured_output:
                print(f"     structured_output: {s.structured_output}")
            break
    else:
        print("\n[..] still running; the reconciler would keep polling.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
