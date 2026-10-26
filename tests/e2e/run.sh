#!/usr/bin/env bash
# The whole loop on a throwaway board: the page in a real browser, the server, the hooks and the CLI.
# Needs Node 22+ and a Chromium: CHROME=/path/to/chrome tests/e2e/run.sh
set -euo pipefail

CHROME=${CHROME:?Set CHROME to a Chrome, Chromium or chrome-headless-shell binary}
HERE=$(cd "$(dirname "$0")" && pwd)
YOURMOVE="$HERE/../../bin/yourmove"
ROOT=$(mktemp -d)
PORT=47690
URL="http://127.0.0.1:$PORT/"
# Screenshots of each step go to $SHOTS when it is set, and are thrown away with the rest otherwise.
SHOTS=${SHOTS:-$ROOT/screenshots}
mkdir -p "$SHOTS"

# Trust, secrets and session state stay inside the throwaway folder.
export XDG_CONFIG_HOME="$ROOT/config" XDG_STATE_HOME="$ROOT/state"
unset CLAUDE_PLUGIN_DATA

server=""
cleanup() {
  [ -n "$server" ] && kill "$server" 2>/dev/null || true
  rm -rf "$ROOT"
}
trap cleanup EXIT

fail() { echo "FAIL $1"; exit 1; }
pass() { echo "PASS $1"; }
# Never session-start here: it can rewrite the login server in the real ~/Library/LaunchAgents, and HOME
# cannot move with it, since version managers such as asdf find python3 through HOME.
hook() { echo "{\"cwd\":\"$ROOT/shop/$2\",\"session_id\":\"$3\"}" | "$YOURMOVE" hook "$1"; }

git -C "$ROOT" init -q -b main shop/shop.wt
git -C "$ROOT/shop/shop.wt" remote add origin git@github.com:acme/shop.git
git -C "$ROOT/shop/shop.wt" -c user.name=t -c user.email=t@t commit -q --allow-empty -m first
git -C "$ROOT/shop/shop.wt" worktree add -q -b feature/checkout ../checkout

cd "$ROOT"
"$YOURMOVE" init --owner Ada >/dev/null
"$YOURMOVE" add --project shop --title Checkout --next "Pick the launch price." --state needs-you \
  --where checkout --choice 'Ship at $25' --choice "Hold a week" >/dev/null
"$YOURMOVE" add --project shop --title Search --next "Build it." --state in-progress >/dev/null

"$YOURMOVE" serve --port "$PORT" >"$ROOT/server.log" 2>&1 &
server=$!
for _ in $(seq 50); do curl -sf "$URL" >/dev/null && break; sleep 0.2; done
LINK="${URL}?token=$(cat "$XDG_CONFIG_HOME/yourmove/server-token")"

node "$HERE/page.mjs" "$CHROME" "$URL" "$SHOTS" locked
node "$HERE/page.mjs" "$CHROME" "$LINK" "$SHOTS" comment

hook prompt checkout s-checkout | grep -q 'Ship at \$25' && pass "the choice reaches the session in its card's worktree" || fail "choice not delivered"
hook prompt shop.wt s-main | grep -q "Use the new copy." && pass "a comment on a card with no worktree reaches the main worktree" || fail "comment not delivered"
[ -z "$(hook prompt checkout s-checkout)" ] && pass "a comment is delivered once" || fail "comment delivered twice"
hook stop shop.wt s-main | grep -q '"decision": "block"' && pass "a stop with an unanswered comment is blocked" || fail "stop not blocked"

choice=$(python3 -c "import json,sys; print(next(json.loads(l)['id'] for l in open(sys.argv[1]) if 'Ship' in l))" "$ROOT/.yourmove/comments.jsonl")
"$YOURMOVE" reply "$choice" 'Shipping at $25.' >/dev/null
[ -z "$(hook stop checkout s-checkout)" ] && pass "a stop after replying goes through" || fail "stop blocked after reply"

node "$HERE/page.mjs" "$CHROME" "$LINK" "$SHOTS" answered

node "$HERE/page.mjs" "$CHROME" "$LINK" "$SHOTS" down &
page=$!
sleep 4
kill "$server"
wait "$server" 2>/dev/null || true
server=""
wait "$page"

TOKEN=$(cat "$XDG_CONFIG_HOME/yourmove/server-token")
grep -q "$TOKEN" "$ROOT/server.log" && fail "the server log holds the token" || pass "the server log never holds the token"

echo "All passed."
