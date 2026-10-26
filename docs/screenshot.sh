#!/usr/bin/env bash
# Remakes docs/board.png: a demo board, served and photographed in headless Chromium.
# CHROME=/path/to/chrome docs/screenshot.sh
set -euo pipefail

CHROME=${CHROME:?Set CHROME to a Chrome, Chromium or chrome-headless-shell binary}
HERE=$(cd "$(dirname "$0")" && pwd)
YOURMOVE="$HERE/../bin/yourmove"
ROOT=$(mktemp -d)
BOARD="$ROOT/.yourmove"
PORT=47691
URL="http://127.0.0.1:$PORT/"

export XDG_CONFIG_HOME="$ROOT/config" XDG_STATE_HOME="$ROOT/state"
unset CLAUDE_PLUGIN_DATA

server=""
cleanup() {
  if [ -n "$server" ]; then kill "$server" 2>/dev/null; wait "$server" 2>/dev/null || true; fi
  rm -rf "$ROOT"
}
trap cleanup EXIT

mkdir -p "$BOARD"
cat >"$BOARD/yourmove.json" <<'JSON'
{
  "owner": "Zach",
  "tickets": "https://linear.app/acme/issue/{id}",
  "projects": {
    "shop": { "name": "Shop", "color": "#1f9d63", "repo": "acme/shop" },
    "app": { "name": "App", "color": "#3b82d6", "repo": "acme/app" },
    "site": { "name": "Site", "color": "#d99a00", "repo": "acme/site" }
  }
}
JSON
touch "$BOARD/cards.jsonl"

add() { "$YOURMOVE" --board "$BOARD" add "$@" >/dev/null; }
add --project shop --state needs-you --title "Gift cards launch price" \
  --next "Checkout is built and tested. Launch at \$25, or hold a week for the holiday promo?" \
  --choice "Launch at \$25" --choice "Hold a week"
add --project app --state needs-you --title "App Store rejection" \
  --next "Apple flagged the sign-in screen. Add Sign in with Apple, or appeal?" \
  --choice "Add Sign in with Apple" --choice "Appeal it"
add --project shop --title "Saved carts" --next "Spec it after gift cards ship."
add --project app --title "Dark mode" --next "Start after the 2.4 release."
add --project shop --state in-progress --title "Search filters" --where search-filters --ticket SHOP-142 \
  --next "Building the price filter. Size and color are done."
add --project app --state in-progress --title "Offline mode" --where offline \
  --next "Queueing writes while offline. Sync on reconnect is next."
add --project shop --state in-review --title "Checkout copy" --pr 212 --ticket SHOP-138 --next "Waiting on checks."
add --project site --state in-review --title "RSS feed" --pr 48 --waiting Sam --next "Sam is reviewing."
add --project app --state needs-testing --title "Push reminders" --pr 97 \
  --next "Try a reminder on a real iPhone before merging."
add --project site --state needs-deploy --title "Faster image loading" --pr 45 \
  --next "Merged. Ships with Friday's deploy."
add --project shop --state needs-deploy --title "Order emails" --pr 207 \
  --next "Merged. Turn the flag on once the email template is approved."
add --project app --state live --title "Onboarding test" --next "Running. Read conversion next Monday."
# One card nobody has checked in a week, to show how stale looks.
python3 - "$BOARD/cards.jsonl" <<'PY'
import json, sys
from datetime import date, timedelta
path = sys.argv[1]
cards = [json.loads(line) for line in open(path)]
for card in cards:
    if card["id"] == "app-dark-mode":
        card["updated"] = (date.today() - timedelta(days=7)).isoformat()
open(path, "w").write("".join(json.dumps(card) + "\n" for card in cards))
PY

"$YOURMOVE" --board "$BOARD" serve --port "$PORT" >/dev/null 2>&1 &
server=$!
for _ in $(seq 50); do curl -sf "$URL" >/dev/null && break; sleep 0.2; done
TOKEN=$(cat "$XDG_CONFIG_HOME/yourmove/server-token-$PORT")

comment=$(curl -sf "${URL}api/comments" -H "Origin: http://127.0.0.1:$PORT" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"card": "app-offline-mode", "text": "Cap the queue at 500 writes."}' |
  python3 -c "import json, sys; print(json.load(sys.stdin)[0]['id'])")
"$YOURMOVE" --board "$BOARD" reply "$comment" "Capped at 500. The oldest write drops first, with a warning." >/dev/null

"$CHROME" --headless=new --no-first-run --hide-scrollbars --window-size=1600,600 --force-device-scale-factor=2 --virtual-time-budget=5000 \
  --user-data-dir="$ROOT/chrome" --screenshot="$HERE/board.png" "${URL}?token=$TOKEN" 2>/dev/null
echo "Wrote $HERE/board.png"
