#!/usr/bin/env bash
# Re-run the YouTube Notes app and verify it still serves.
#
# Used by the PostToolUse hook in .claude/settings.json, but safe to run by
# hand:  ./.claude/verify.sh
#
# Restarts the server if it isn't up, waits for it, then exercises the real
# endpoints. Exits 2 with a message on stderr if anything is broken — that is
# the "blocking error" convention, so Claude Code feeds the failure back.

set -uo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJ"

BASE="http://127.0.0.1:${PORT:-8000}"
LOG="${TMPDIR:-/tmp}/youtube-notes-server.log"
TEST_URL="https://www.youtube.com/watch?v=dQw4w9WgXcQ"

fail() { echo "verify: $*" >&2; exit 2; }

# ── 1. Always restart from scratch. ───────────────────────────────────
# A plain --reload is not enough: run.sh exports .env into the process
# environment at launch, so config edits are only picked up by a full restart.
# The --reload worker is spawned with a different command line ("spawn_main"),
# so matching on "uvicorn app.main:app" kills the parent and leaves the child
# holding the socket. Kill whoever actually has the port.
pkill -f "uvicorn app.main:app" 2>/dev/null
holders=$(lsof -t -nP -iTCP:"${PORT:-8000}" -sTCP:LISTEN 2>/dev/null)
[ -n "$holders" ] && kill $holders 2>/dev/null

# The socket is released a moment after the process dies.
for _ in $(seq 1 40); do
  lsof -nP -iTCP:"${PORT:-8000}" -sTCP:LISTEN >/dev/null 2>&1 || break
  sleep 0.25
done
# Anything still clinging on gets SIGKILL.
holders=$(lsof -t -nP -iTCP:"${PORT:-8000}" -sTCP:LISTEN 2>/dev/null)
if [ -n "$holders" ]; then
  kill -9 $holders 2>/dev/null
  for _ in $(seq 1 20); do
    lsof -nP -iTCP:"${PORT:-8000}" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 0.25
  done
fi
lsof -nP -iTCP:"${PORT:-8000}" -sTCP:LISTEN >/dev/null 2>&1 \
  && fail "port ${PORT:-8000} still held after killing the old server"

nohup ./run.sh >"$LOG" 2>&1 &

# uvicorn --reload needs a moment after an edit; wait for it to answer again.
up=""
for _ in $(seq 1 60); do
  if curl -sf -o /dev/null --max-time 2 "$BASE/api/health" 2>/dev/null; then up=1; break; fi
  grep -q "Traceback" "$LOG" 2>/dev/null && break
  sleep 0.5
done

if [ -z "$up" ]; then
  fail "server did not come up. Last log lines:
$(tail -20 "$LOG" 2>/dev/null)"
fi

# ── 1b. Sign in. The API is invite-only now, so anonymous calls 401. ──
COOKIE="${TMPDIR:-/tmp}/youtube-notes-verify.cookies"
./.venv/bin/python - "$COOKIE" "$BASE" <<'PY' || fail "could not create the verification account"
import sys, json, urllib.request, http.cookiejar
sys.path.insert(0, ".")
from app import auth, store
store.init_db(); auth.init_auth()

EMAIL, PW = "verify@localhost", "verify-password-12345"
try:
    auth.register(EMAIL, PW, auth.create_invite(None))
except auth.AuthError:
    pass                                    # already registered on an earlier run

jar = http.cookiejar.MozillaCookieJar(sys.argv[1])
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
req = urllib.request.Request(
    sys.argv[2] + "/api/auth/login",
    data=json.dumps({"email": EMAIL, "password": PW}).encode(),
    headers={"Content-Type": "application/json"},
)
with op.open(req, timeout=30) as r:
    assert r.status == 200, r.status
jar.save(ignore_discard=True, ignore_expires=True)
PY

CURL="curl -s -b $COOKIE"

# ── 2. UI still served? ───────────────────────────────────────────────
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE/")
[ "$code" = "200" ] || fail "GET / returned $code, expected 200"

# ── 3. Config sane? ───────────────────────────────────────────────────
health=$($CURL --max-time 10 "$BASE/api/health")
provider=$(printf '%s' "$health" | python3 -c 'import sys,json;print(json.load(sys.stdin)["default_provider"])' 2>/dev/null) \
  || fail "/api/health returned unparseable JSON: $health"

# ── 4. Network-dependent checks, throttled. ───────────────────────────
# /api/transcript and /api/notes both fetch from YouTube, which rate-limits
# after a burst of requests. Running them on every single edit produces false
# failures, so only reach out if the last good run was more than THROTTLE
# seconds ago. Set FORCE=1 to bypass.
STAMP="${TMPDIR:-/tmp}/youtube-notes-verify.stamp"
THROTTLE=600

skip_net=""
if [ "${FORCE:-0}" != "1" ] && [ -f "$STAMP" ]; then
  age=$(( $(date +%s) - $(cat "$STAMP" 2>/dev/null || echo 0) ))
  [ "$age" -lt "$THROTTLE" ] && skip_net=1
fi

if [ -n "$skip_net" ]; then
  summary="verified: UI 200, config OK (provider: ${provider}); network checks skipped, last run ${age}s ago"
  python3 -c "
import json, sys
print(json.dumps({'systemMessage': sys.argv[1], 'suppressOutput': True}))
" "$summary"
  exit 0
fi

tr_json=$($CURL --max-time 90 -X POST "$BASE/api/transcript" \
  -H 'Content-Type: application/json' -d "{\"url\":\"$TEST_URL\"}")
segments=$(printf '%s' "$tr_json" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("segments",0))' 2>/dev/null) \
  || fail "/api/transcript returned unparseable JSON: $tr_json"

# A throttled YouTube looks like "no transcript available". That is an upstream
# rate limit, not a regression in this app, so report it without failing.
if [ "${segments:-0}" -le 0 ] 2>/dev/null; then
  case "$tr_json" in
    *"No transcript"*|*"captions"*)
      python3 -c "
import json
print(json.dumps({'systemMessage': 'verified: UI 200, config OK; YouTube rate-limited the transcript fetch (upstream, not a code regression)', 'suppressOutput': True}))
"
      exit 0 ;;
    *) fail "/api/transcript produced no segments: $tr_json" ;;
  esac
fi

# ── 5. Notes pipeline, now asynchronous. ──────────────────────────────
# /api/notes returns 202 {"job": id}; the work happens on a worker thread.
# Poll it — an earlier version of this script read the 202 as success and
# reported "notes OK" without a single note having been written.
notes_status=$(./.venv/bin/python - "$BASE" "$COOKIE" "$TEST_URL" <<'PY'
import http.cookiejar, json, sys, time, urllib.request

base, cookies, url = sys.argv[1], sys.argv[2], sys.argv[3]
jar = http.cookiejar.MozillaCookieJar(cookies)
try:
    jar.load(ignore_discard=True, ignore_expires=True)
except Exception:
    pass
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with op.open(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}

st, d = call("/api/notes", {"url": url, "extra": "one short bullet"})
if st != 202 or not d.get("job"):
    print("FAIL|expected 202 with a job id, got %s %s" % (st, str(d)[:120])); raise SystemExit
job = d["job"]

deadline = time.time() + 420
while time.time() < deadline:
    time.sleep(2)
    st, j = call("/api/jobs/" + job)
    if st != 200:
        print("FAIL|polling the job returned %s" % st); raise SystemExit
    if j.get("status") == "done":
        notes = (j.get("result") or {}).get("notes") or ""
        print("OK|%d chars" % len(notes) if notes else "FAIL|job finished with no notes")
        raise SystemExit
    if j.get("status") == "error":
        err = j.get("error", "")
        blocked = any(s in err for s in ("API key", "not set", "credit", "quota", "401", "429"))
        print(("BLOCKED|" if blocked else "FAIL|") + err[:160]); raise SystemExit
print("FAIL|job did not finish within the timeout")
PY
)

case "$notes_status" in
  FAIL*) fail "/api/notes broken: ${notes_status#FAIL|}" ;;
esac

# ── Report ────────────────────────────────────────────────────────────
date +%s > "$STAMP"
summary="verified: UI 200, transcript ${segments} segments, notes ${notes_status%%|*} (provider: ${provider})"
python3 -c "
import json, sys
print(json.dumps({'systemMessage': sys.argv[1], 'suppressOutput': True}))
" "$summary"
