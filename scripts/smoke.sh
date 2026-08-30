#!/usr/bin/env bash
# Pre-demo smoke check (TESTING.md). Usage: scripts/smoke.sh [BASE_URL] [ROOM]
# Resets the room (destroys its sandbox), then verifies sandbox, UI, seed tasks.
# Check 4 (both agents connected) is verified by eye on the spectator UI after they join.
set -u
BASE="${1:-http://localhost:8000}"
ROOM="${2:-demo}"
H='-H ngrok-skip-browser-warning:1'
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILED=1; }
FAILED=0
echo "Smoke: $BASE room=$ROOM"

# 1. clean room
if curl -sf $H -X POST "$BASE/admin/reset/$ROOM" >/dev/null; then pass "reset $ROOM"; else fail "reset $ROOM"; fi

# 2. sandbox reachable: join (creates + seeds sandbox), run pytest, expect a real exit code
JOIN=$(curl -sf $H -X POST "$BASE/tools/join_room" -H 'content-type: application/json' \
  -d "{\"room\":\"$ROOM\",\"agent_name\":\"smoke\",\"agent_kind\":\"sim\"}")
AID=$(printf '%s' "$JOIN" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("agent_id",""))' 2>/dev/null)
if [ -z "$AID" ]; then fail "join_room (sandbox creation): $JOIN"; else
  RUN=$(curl -sf $H -X POST "$BASE/tools/run" -H 'content-type: application/json' \
    -d "{\"agent_id\":\"$AID\",\"command\":\"pytest -q\"}")
  SUMMARY=$(printf '%s' "$RUN" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["exit_code"], d["stdout"].strip().splitlines()[-1] if d["stdout"].strip() else "")' 2>/dev/null)
  case "$SUMMARY" in
    "0 "*) pass "sandbox: pytest -> $SUMMARY" ;;
    *) fail "sandbox: pytest -> ${SUMMARY:-$RUN}" ;;
  esac
fi

# 3. spectator UI loads and API serves the board
UI=$(curl -s $H -o /dev/null -w '%{http_code}' "$BASE/ui/$ROOM")
[ "$UI" = "200" ] && pass "UI /ui/$ROOM -> 200" || fail "UI /ui/$ROOM -> $UI"

# 5. seed tasks present, all open
TASKS=$(curl -sf $H "$BASE/api/board/$ROOM" | python3 -c 'import sys,json; d=json.load(sys.stdin); t=d["tasks"]; print(len(t), sum(1 for x in t if x["status"]=="open"))' 2>/dev/null)
case "$TASKS" in
  "0 "*|"") fail "seed tasks: none (${TASKS:-api error})" ;;
  *) set -- $TASKS; [ "$1" = "$2" ] && pass "seed tasks: $1 present, all open" || fail "seed tasks: $1 present, only $2 open" ;;
esac

# the smoke agent should not linger on the board
curl -sf $H -X POST "$BASE/tools/handoff" -H 'content-type: application/json' \
  -d "{\"agent_id\":\"$AID\",\"summary\":\"smoke check\",\"next_steps\":\"none\",\"blockers\":\"\"}" >/dev/null 2>&1

echo
if [ "$FAILED" = 0 ]; then
  echo "ALL GOOD. 4) now connect both agents (cd demo; /mcp) and confirm they appear on $BASE/ui/$ROOM"
else
  echo "SMOKE FAILED — play the backup video. Do not debug on stage."; exit 1
fi
