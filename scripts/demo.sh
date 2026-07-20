#!/usr/bin/env bash
# End-to-end demo. Runs fully offline in mock mode -- no Devin key, no GitHub
# token, no network. Add credentials to .env and it does the same thing for real.
set -euo pipefail

HOST="${HOST:-http://localhost:8000}"
say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
poll() { curl -fsS "$HOST/metrics"; }

say "Waiting for the service"
for _ in $(seq 1 30); do
  if curl -fsS "$HOST/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "$HOST/healthz" | python3 -m json.tool

say "1. Scheduled sweep fires (discovery is sweep-only by design)"
# The quiet-period gate waits days for a PR to go stale, so push-webhook
# latency buys nothing for detection -- the sweep alone carries discovery.
curl -fsS -XPOST "$HOST/simulate" | python3 -m json.tool

say "2. Guardrail: the same event aimed at upstream apache/superset"
curl -s -o /dev/stdout -w '\nHTTP %{http_code}\n' -XPOST "$HOST/webhook/github" \
  -H 'X-GitHub-Event: issues' -H 'Content-Type: application/json' \
  -d '{"action":"labeled","label":{"name":"devin-unblock"},
       "issue":{"number":1,"title":"Unblock PR #22604: x"},
       "repository":{"full_name":"apache/superset"}}' || true

say "3. A maintainer labels three more tracking issues"
for pr in 29503 31981 32487; do
  printf '   labelling issue for PR #%s ... ' "$pr"
  curl -fsS -XPOST "$HOST/webhook/github" \
    -H 'X-GitHub-Event: issues' -H 'Content-Type: application/json' \
    -d '{"action":"labeled","label":{"name":"devin-unblock"},
         "issue":{"number":900,"title":"Unblock PR #'"$pr"': demo"},
         "repository":{"full_name":"'"${GITHUB_REPO:-raymondtangsc/superset}"'"}}' >/dev/null
  echo ok
done

say "4. Waiting for sessions to reach terminal states"
for _ in $(seq 1 12); do
  in_flight=$(poll | python3 -c 'import json,sys;print(json.load(sys.stdin)["in_flight"])')
  dispatched=$(poll | python3 -c 'import json,sys;d=json.load(sys.stdin)["by_state"];print(d.get("dispatched",0)+d.get("running",0))')
  printf '   in flight: %s (sessions running: %s)\n' "$in_flight" "$dispatched"
  [ "$dispatched" = "0" ] && break
  sleep 5
done

say "5. Results"
poll | python3 -c '
import json, sys
m = json.load(sys.stdin)
r = m["success_rate"]
rows = [
    ("tracked",          m["total_tracked"]),
    ("unblocked",        m["succeeded"]),
    ("needing a human",  m["failed"]),
    ("success rate",     "-" if r is None else "%.0f%%" % (r * 100)),
    ("median unblock",   "%ss" % m["median_unblock_seconds"]),
    ("ACUs per success", m["acus_per_success"]),
    ("blockers",         m["blocker_mix"]),
]
for k, v in rows:
    print("  %-18s %s" % (k, v))
'

printf '\nDashboard: %s\n' "$HOST/"
