"""GitHub sync, the board server, and the command line."""

from __future__ import annotations

import contextlib
import io
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from support import LONG_AGO, TODAY, BoardCase, card
from yourmove import board as boards
from yourmove import cli, github, server


class Sync(BoardCase):
    def plan(self, states: dict[int, str]) -> tuple[list[github.Move], list[str]]:
        def gh(*args: str) -> str | None:
            if args[:2] == ("pr", "list"):
                return json.dumps([{"number": n, "state": s} for n, s in states.items() if n < 100])
            number = int(args[2])
            return json.dumps({"state": states[number]}) if number in states else None

        with mock.patch.object(github, "gh", gh):
            return github.plan(self.board)

    def test_merged_and_opened_prs_move_their_cards(self) -> None:
        self.write_cards(
            card(id="merged", state="in-review", pr=1),
            card(id="opened", state="in-progress", pr=2),
            card(id="old-merge", state="needs-testing", pr=500),
        )
        moves, unclear = self.plan({1: "MERGED", 2: "OPEN", 500: "MERGED"})
        self.assertEqual(
            {(m.card["id"], m.to) for m in moves},
            {("merged", "needs-deploy"), ("opened", "in-review"), ("old-merge", "needs-deploy")},
        )
        self.assertEqual(unclear, [])

    def test_an_unreachable_repo_is_asked_once_and_reported(self) -> None:
        self.write_cards(card(id="a", pr=1), card(id="b", pr=2))
        calls = []

        def gh(*args: str) -> str | None:
            calls.append(args)
            return None

        with mock.patch.object(github, "gh", gh):
            moves, unclear = github.plan(self.board)
        self.assertEqual((len(calls), moves, len(unclear)), (1, [], 2))

    def test_a_planning_card_listing_prs_it_will_rebuild_stays(self) -> None:
        self.write_cards(card(id="later", state="planning", pr=[1, 2]))
        self.assertEqual(self.plan({1: "OPEN", 2: "MERGED"})[0], [])

    def test_a_card_waiting_on_the_owner_or_already_past_stays(self) -> None:
        self.write_cards(card(id="ask", state="needs-you", pr=1), card(id="done", state="live", pr=2))
        self.assertEqual(self.plan({1: "MERGED", 2: "MERGED"})[0], [])

    def test_a_card_is_merged_only_when_all_its_prs_are(self) -> None:
        self.write_cards(card(state="in-review", pr=[1, 2]))
        self.assertEqual(self.plan({1: "MERGED", 2: "OPEN"})[0], [])

    def test_a_closed_pr_or_an_unreadable_one_is_left_to_a_person(self) -> None:
        self.write_cards(card(id="closed", pr=1), card(id="unknown", pr=700))
        moves, unclear = self.plan({1: "CLOSED"})
        self.assertEqual(moves, [])
        self.assertEqual(len(unclear), 2)

    def test_applying_moves_keeps_updated_and_skips_cards_that_moved_meanwhile(self) -> None:
        self.write_cards(card(id="a", state="in-review", pr=1, updated=LONG_AGO), card(id="b", state="in-review", pr=2))
        moves, _ = self.plan({1: "MERGED", 2: "MERGED"})
        boards.update(self.board, "b", {"state": "needs-you"}, by="test")
        made = github.apply(self.board, moves)
        self.assertEqual([m.card["id"] for m in made], ["a"])
        cards = {c["id"]: c for c in boards.read(self.board).cards}
        self.assertEqual((cards["a"]["state"], cards["a"]["updated"]), ("needs-deploy", LONG_AGO))
        self.assertEqual(cards["b"]["state"], "needs-you")
        self.assertEqual(boards.history(self.board)[-1]["by"], "sync")


class Server(BoardCase):
    def setUp(self) -> None:
        super().setUp()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        handler = server.handler_for(self.dir, self.port)
        handler.log_message = lambda *args: None  # type: ignore[method-assign]
        httpd = HTTPServer(("127.0.0.1", self.port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.write_cards(card(where="checkout"), card(id="bad", state="nope"))

    def call(self, method: str, path: str, body: Any = None, origin: str | None = "own") -> tuple[int, Any]:
        url = f"http://127.0.0.1:{self.port}{path}"
        request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if origin:
            request.add_header("Origin", f"http://127.0.0.1:{self.port}" if origin == "own" else origin)
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                return response.status, json.loads(raw) if path.startswith("/api") else raw
        except urllib.error.HTTPError as error:
            return error.code, None

    def test_the_board_comes_with_cards_errors_and_projects(self) -> None:
        status, answer = self.call("GET", "/api/board")
        self.assertEqual(status, 200)
        self.assertEqual([c["id"] for c in answer["cards"]], ["shop-checkout"])
        self.assertIn("unknown state", answer["errors"][0])
        self.assertEqual(answer["projects"]["shop"]["repo"], "acme/shop")
        self.assertEqual(answer["owner"], "Ada")

    def test_the_page_is_served(self) -> None:
        status, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Board</title>", page)

    def test_the_page_can_comment_and_delete(self) -> None:
        status, comments = self.call("POST", "/api/comments", {"card": "shop-checkout", "text": "Go."})
        self.assertEqual(status, 201)
        self.assertEqual((comments[0]["where"], comments[0]["title"]), ("checkout", "Checkout"))
        status, comments = self.call("DELETE", f"/api/comments/{comments[0]['id']}")
        self.assertEqual((status, comments), (200, []))

    def test_another_site_cannot_speak_as_the_owner(self) -> None:
        self.assertEqual(
            self.call("POST", "/api/comments", {"card": "shop-checkout", "text": "x"}, origin="https://evil.example")[
                0
            ],
            403,
        )
        self.assertEqual(
            self.call("POST", "/api/comments", {"card": "shop-checkout", "text": "x"}, origin=None)[0], 403
        )
        self.assertEqual(boards.read_lines(self.dir / boards.COMMENTS_FILE), [])

    def test_a_comment_on_an_unknown_card_or_with_no_text_is_refused(self) -> None:
        self.assertEqual(self.call("POST", "/api/comments", {"card": "nope", "text": "x"})[0], 400)
        self.assertEqual(self.call("POST", "/api/comments", {"card": "shop-checkout", "text": " "})[0], 400)

    def test_a_broken_config_is_shown_on_the_page(self) -> None:
        (self.dir / boards.CONFIG_FILE).write_text("{")
        self.assertEqual(self.call("GET", "/api/board")[0], 500)


class CommandLine(BoardCase):
    def cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["--board", str(self.dir), *args])
        return code, out.getvalue()

    def test_add_set_and_ls_round_trip(self) -> None:
        self.assertEqual(self.cli("add", "--project", "shop", "--title", "Gift cards", "--next", "Spec it.")[0], 0)
        code, _ = self.cli(
            "set", "shop-gift-cards", "--state", "needs-you", "--next", "Price?", "--choice", "$10", "--choice", "$25"
        )
        self.assertEqual(code, 0)
        code, listing = self.cli("ls", "--state", "needs-you")
        self.assertIn("shop-gift-cards  Gift cards", listing)
        self.assertEqual(boards.read(self.board).find("shop-gift-cards")["choices"], ["$10", "$25"])

    def test_clear_removes_a_field_and_repeated_prs_make_a_list(self) -> None:
        self.write_cards(card(waiting="Sam"))
        self.cli("set", "shop-checkout", "--clear", "waiting", "--pr", "1", "--pr", "2")
        updated = boards.read(self.board).find("shop-checkout")
        self.assertNotIn("waiting", updated)
        self.assertEqual(updated["pr"], [1, 2])

    def test_a_refused_write_explains_itself_and_exits_1(self) -> None:
        self.write_cards(card())
        code, output = self.cli("set", "shop-checkout", "--next", "x" * 400)
        self.assertEqual(code, 1)
        self.assertIn("limit is 280", output)

    def test_lint_fails_only_on_errors(self) -> None:
        self.write_cards(card(next="x" * 300))
        code, output = self.cli("lint")
        self.assertEqual(code, 0)
        self.assertIn("1 warnings", output)
        self.write_lines(["{bad"])
        self.assertEqual(self.cli("lint")[0], 1)

    def test_touch_and_log(self) -> None:
        self.write_cards(card(updated=LONG_AGO))
        self.cli("touch", "shop-checkout")
        self.assertEqual(boards.read(self.board).find("shop-checkout")["updated"], TODAY)
        self.cli("set", "shop-checkout", "--state", "in-review")
        self.assertIn("in-progress -> in-review", self.cli("log")[1])

    def test_init_makes_a_board_from_the_repo_it_is_run_in(self) -> None:
        main = self.repo("shop", "acme/shop")
        target = main / boards.DIR_NAME
        with mock.patch.object(Path, "cwd", return_value=main):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--board", str(target), "init", "--owner", "Ada"]), 0)
        made = boards.load(target)
        self.assertEqual((made.owner, made.projects["shop"].repo), ("Ada", "acme/shop"))
        self.assertEqual(cli.main(["--board", str(target), "init"]), 1)

    def test_the_hook_command_reads_its_payload_from_stdin(self) -> None:
        main = self.repo("shop", "acme/shop")
        payload = io.StringIO(json.dumps({"cwd": str(main), "session_id": "s"}))
        out = io.StringIO()
        with mock.patch("sys.stdin", payload), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["hook", "session-start"]), 0)
        self.assertIn("Ada's yourmove board", out.getvalue())
