"""Serves the board page on localhost, saves the owner's comments, and keeps PR states in sync."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import board as boards
from . import comments as notes
from . import github, user
from .board import Board, BoardError

PAGE = Path(__file__).with_name("board.html")
MAX_BODY_BYTES = 8192
SYNC_MINUTES = 10
# The yourmove mark, docs/icon.svg, cropped to its tile. The y is drawn, not cut out, so it shows on dark tabs too.
ICON = (
    b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='144 144 736 736'>"
    b"<path fill='#151412' d='M288 144H736Q880 144 880 288V736Q880 880 736 880H288"
    b"Q144 880 144 736V288Q144 144 288 144Z'/>"
    b"<path fill='#f4f2ee' d='M320 288H448V408Q448 456 512 456Q576 456 576 408V288H704V624Q704 784 544 784H416V672H528"
    b"Q576 672 576 624V568Q544 576 512 576Q320 576 320 408Z'/></svg>"
)


def snapshot(board: Board) -> dict[str, Any]:
    """Everything the page draws. Cards are validated here, so the page and lint never disagree."""
    cards = boards.read(board)
    return {
        "owner": board.owner,
        "today": date.today().isoformat(),
        "states": boards.STATES,
        "staleAfterDays": boards.STALE_AFTER_DAYS,
        "projects": {key: vars(project) for key, project in board.projects.items()},
        # Made here, so the page and the briefing never build a ticket link two ways.
        "ticketLinks": {card["ticket"]: board.ticket_link(card["ticket"]) for card in cards.cards if "ticket" in card},
        "cards": cards.cards,
        "errors": [problem.message for problem in cards.errors],
        "comments": notes.load(board),
    }


def handler_for(directory: Path, port: int, token: str) -> type[BaseHTTPRequestHandler]:
    own_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    own_origins = {f"http://{host}" for host in own_hosts}
    authorization = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if self.headers.get("Host") not in own_hosts:
                self.send_error(404)
            elif path == "/":
                self.respond(200, "text/html; charset=utf-8", PAGE.read_bytes())
            elif path == "/favicon.svg":
                self.respond(200, "image/svg+xml", ICON)
            elif path == "/api/proof":
                # Lets `yourmove open` check this is the owner's server before it hands over the token.
                challenge = parse_qs(urlsplit(self.path).query).get("challenge", [""])[0]
                if not re.fullmatch(r"[0-9a-f]{64}", challenge):
                    self.send_error(400)
                    return
                self.respond_json(200, {"proof": proof(token, challenge)})
            elif path == "/api/board":
                if not self.has_token():
                    self.respond_json(401, {"error": "This page needs the board's link. Run `yourmove open`."})
                    return
                # Loaded per request, so an edit to the config shows without a restart.
                try:
                    self.respond_json(200, snapshot(boards.load(directory)))
                except BoardError as error:
                    self.respond_json(500, {"error": str(error)})
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if self.path != "/api/comments" or not self.is_from_the_page() or not self.has_token():
                self.send_error(403)
                return
            if self.headers.get("Content-Type") != "application/json":
                self.send_error(415)
                return
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= MAX_BODY_BYTES:
                self.send_error(413)
                return
            board = boards.load(directory)
            try:
                body = json.loads(self.rfile.read(length))
                card = boards.read(board).find(str(body["card"]))
                comments = notes.add(board, card, str(body["text"]))
            except (ValueError, KeyError, TypeError, BoardError):
                self.send_error(400)
                return
            self.respond_json(201, comments)

        def do_DELETE(self) -> None:
            if not self.path.startswith("/api/comments/") or not self.is_from_the_page() or not self.has_token():
                self.send_error(403)
                return
            self.respond_json(200, notes.remove(boards.load(directory), self.path.removeprefix("/api/comments/")))

        # A comment reaches Claude sessions as the owner's own words, so only this page may write one.
        # Host stops DNS rebinding. Origin stops another site posting here from the owner's browser.
        def is_from_the_page(self) -> bool:
            return self.headers.get("Host") in own_hosts and self.headers.get("Origin") in own_origins

        # Anything on this machine can send any header, so Host and Origin only stop browsers.
        # The token, readable by the owner alone, stops the other accounts on a shared machine.
        def has_token(self) -> bool:
            return hmac.compare_digest(self.headers.get("Authorization", "").encode(), authorization)

        def respond_json(self, status: int, value: Any) -> None:
            self.respond(status, "application/json", json.dumps(value, ensure_ascii=False).encode())

        def respond(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # The page polls. Only writes and errors are worth a log line.
        def log_request(self, code: Any = "-", size: Any = "-") -> None:
            if self.command != "GET":
                super().log_request(code, size)

    return Handler


def proof(token: str, challenge: str) -> str:
    return hmac.new(token.encode(), challenge.encode(), hashlib.sha256).hexdigest()


def is_owners_server(port: int, token: str) -> bool:
    """Whether what answers on the port holds the token, without the token ever leaving this process."""
    challenge = secrets.token_hex(32)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/proof?challenge={challenge}", timeout=5) as response:
            answer = json.loads(response.read())
    except (OSError, ValueError):
        return False
    return isinstance(answer, dict) and hmac.compare_digest(str(answer.get("proof", "")), proof(token, challenge))


def keep_in_sync(directory: Path) -> None:
    reported: set[str] = set()
    while True:
        try:
            board = boards.load(directory)
            moves, unclear = github.plan(board)
            for move in github.apply(board, moves):
                print(f"sync: {move.card['id']} {move.card['state']} -> {move.to} ({move.why})", flush=True)
            # A signed-out gh would otherwise stop the sync without a word. Each problem is logged once.
            for note in sorted(set(unclear) - reported):
                print(f"sync: {note}", flush=True)
            reported = set(unclear)
        except BoardError as error:
            print(f"sync: {error}", flush=True)
        time.sleep(SYNC_MINUTES * 60)


def serve(board: Board, port: int) -> None:
    if github.available():
        threading.Thread(target=keep_in_sync, args=(board.dir,), daemon=True).start()
    # The link holds the token, so it is never printed here: the log can sit in a folder others can read.
    print(
        f"The board is on http://127.0.0.1:{port}/. Open it with {boards.open_command(port)} (Ctrl-C to stop).",
        flush=True,
    )
    with contextlib.suppress(KeyboardInterrupt):
        HTTPServer(("127.0.0.1", port), handler_for(board.dir, port, user.new_server_token(port))).serve_forever()
