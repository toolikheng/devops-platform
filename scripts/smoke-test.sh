#!/usr/bin/env bash
# smoke-test.sh - prove a running PolicyPal works end to end.
#
# Usage: smoke-test.sh <ui-url>   e.g. http://policypal.localhost:8080 (k3d) or http://<node-ip> (AWS)
#
# The API has no ingress, so it is called through `kubectl port-forward` to its Service, with kubectl's own
# credentials: nothing is opened on the node. (`kubectl create --raw` through the API server's service proxy
# was tried first: it sends no Content-Type, and FastAPI rejects the JSON body with a 422.) kubectl picks a
# free local port and prints it; with a fixed port, another forward already listening there (perhaps to
# another cluster) would answer instead, and the checks would pass for the wrong cluster. The UI is called
# through the ingress, like a browser, which also shows that the API's Swagger page and schema are NOT
# reachable that way. Two questions come from PolicyPal's golden set: g04 must be answered from the Leave
# Policy section 6.3, g41 must be refused. Each /ask is stored in PolicyPal's question history. Stops if the
# port-forward doesn't start; otherwise runs every check, then exits 1 if any failed.
set -euo pipefail

ui="${1:-}"
if ! [[ "$ui" =~ ^https?://[^/]+ ]]; then
  echo "usage: smoke-test.sh <ui-url>" >&2
  exit 2
fi
ui="${ui%/}"

G04="How many days of compassionate leave do I get when my grandmother passes away?"
G41="How many months of bonus will we get at the end of the year?"

failures=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1" >&2; failures=$((failures + 1)); }
# check <description> <jq condition> <json>
check() {
  if jq -e "$2" >/dev/null 2>&1 <<<"$3"; then pass "$1"; else fail "$1"; fi
}
ask() {
  curl -fsS --max-time 60 -H "Content-Type: application/json" -d "$(jq -n --arg q "$1" '{question: $q}')" "$api/ask"
}

forward_log="$(mktemp)"
kubectl -n policypal port-forward svc/policypal-api :8000 >"$forward_log" 2>&1 &
forward=$!
trap 'kill "$forward" 2>/dev/null || true; rm -f "$forward_log"' EXIT
# kubectl prints "Forwarding from 127.0.0.1:<port> -> 8000" once it listens. Wait up to 10 s for it, or
# until kubectl exits (no such Service, cluster unreachable).
port=""
for _ in $(seq 20); do
  # Checked before reading the log, so a kubectl that has exited has also finished writing it.
  exited=""
  kill -0 "$forward" 2>/dev/null || exited=yes
  port="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9]*\) .*/\1/p' "$forward_log")"
  if [ -n "$port" ] || [ -n "$exited" ]; then break; fi
  sleep 0.5
done
if [ -z "$port" ]; then
  echo "FAIL  kubectl port-forward to the API did not start: $(head -c 300 "$forward_log")" >&2
  exit 1
fi
api="http://127.0.0.1:$port"

if health="$(curl -fsS --max-time 10 "$api/health")"; then
  check "API /health: ok, extractive mode, policies loaded" \
    '.status == "ok" and .llm.provider == "extractive" and .documents > 0 and .files_not_loaded == []' "$health"
else
  fail "API /health did not answer 200 (503: the database is unreachable or not migrated)"
fi

if answer="$(ask "$G04")"; then
  check "g04 answered from Leave Policy 6.3, without an LLM" \
    '.mode == "extractive" and any(.citations[]; .document_title == "Leave Policy" and (.section | startswith("6.3")))' \
    "$answer"
else
  fail "g04: /ask failed"
fi

if answer="$(ask "$G41")"; then
  check "g41 refused (bonus is not in any policy)" '.refused == true' "$answer"
else
  fail "g41: /ask failed"
fi

if [ "$(curl -fsS --max-time 10 "$ui/_stcore/health")" = "ok" ]; then
  pass "UI healthy through the ingress ($ui)"
else
  fail "UI not healthy through the ingress ($ui/_stcore/health)"
fi

# The ingress routes everything to the UI, so these paths must never return the API's pages.
if grep -qi 'swagger' <<<"$(curl -sS --max-time 10 "$ui/docs" || true)"; then
  fail "API Swagger page reachable through the ingress ($ui/docs)"
else
  pass "API Swagger page not reachable through the ingress"
fi
if grep -q '"openapi"' <<<"$(curl -sS --max-time 10 "$ui/openapi.json" || true)"; then
  fail "API schema reachable through the ingress ($ui/openapi.json)"
else
  pass "API schema not reachable through the ingress"
fi

if [ "$failures" -gt 0 ]; then
  echo "smoke-test: $failures check(s) failed" >&2
  exit 1
fi
echo "smoke-test: all checks passed"
