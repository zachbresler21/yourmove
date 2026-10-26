"""GitHub sync, the board server, and the command line."""

from __future__ import annotations

import contextlib
import io
import json
import plistlib
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from support import LONG_AGO, TODAY, BoardCase, card
from yourmove import autostart, cli, github, server, user
from yourmove import board as boards


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


class Stopped(Exception):
    pass


class SyncLoop(BoardCase):
    @mock.patch("yourmove.github.gh", return_value=None)
    @mock.patch("time.sleep", side_effect=[None, None, Stopped])
    def test_a_problem_is_logged_once_not_every_round(self, sleep: mock.MagicMock, gh: mock.MagicMock) -> None:
        self.write_cards(card(pr=1))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(Stopped):
            server.keep_in_sync(self.dir)
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual(out.getvalue().count("could not read PR state"), 1)


class Server(BoardCase):
    def setUp(self) -> None:
        super().setUp()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        handler = server.handler_for(self.dir, self.port, user.new_server_token(self.port))
        handler.log_message = lambda *args: None  # type: ignore[method-assign]
        httpd = HTTPServer(("127.0.0.1", self.port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.write_cards(card(where="checkout"), card(id="bad", state="nope"))

    def call(
        self, method: str, path: str, body: Any = None, origin: str | None = "own", token: str | None = "own"
    ) -> tuple[int, Any]:
        url = f"http://127.0.0.1:{self.port}{path}"
        request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {user.server_token(self.port) if token == 'own' else token}")
        if origin:
            request.add_header("Origin", f"http://127.0.0.1:{self.port}" if origin == "own" else origin)
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                return response.status, json.loads(raw) if path.startswith("/api") else raw
        except urllib.error.HTTPError as error:
            with error:
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

    def test_another_account_on_this_machine_gets_nothing_without_the_token(self) -> None:
        for token in (None, "0" * 64, "é"):
            self.assertEqual(self.call("GET", "/api/board", token=token)[0], 401)
            self.assertEqual(
                self.call("POST", "/api/comments", {"card": "shop-checkout", "text": "x"}, token=token)[0], 403
            )
            self.assertEqual(self.call("DELETE", "/api/comments/abc", token=token)[0], 403)
        self.assertEqual(boards.read_lines(self.dir / boards.COMMENTS_FILE), [])

    def test_open_hands_the_browser_the_token_through_a_private_file_never_a_command_line(self) -> None:
        out = io.StringIO()
        with mock.patch("webbrowser.open") as browser, contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--board", str(self.dir), "open", "--port", str(self.port)]), 0)
        forward = self.root / "config" / "yourmove" / "open.html"
        browser.assert_called_once_with(forward.as_uri())
        token = user.server_token(self.port)
        self.assertIn(f"http://127.0.0.1:{self.port}/?token={token}", forward.read_text())
        self.assertEqual(forward.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(token, out.getvalue())

    def test_open_hands_nothing_to_a_server_that_cannot_prove_it_holds_the_token(self) -> None:
        # Another account's server on the port, as when it took the port while the owner's was down.
        user.new_server_token(self.port)
        err = io.StringIO()
        with mock.patch("webbrowser.open") as browser, contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--board", str(self.dir), "open", "--port", str(self.port)]), 1)
        browser.assert_not_called()
        self.assertIn("not running on port", err.getvalue())
        self.assertFalse((self.root / "config" / "yourmove" / "open.html").exists())

    def test_open_with_nothing_on_the_port_says_to_start_the_server(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        err = io.StringIO()
        with mock.patch("webbrowser.open") as browser, contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--board", str(self.dir), "open", "--port", str(free)]), 1)
        browser.assert_not_called()
        self.assertIn("yourmove serve", err.getvalue())

    def test_a_bad_challenge_gets_no_proof(self) -> None:
        self.assertEqual(self.call("GET", "/api/proof?challenge=short", token=None)[0], 400)

    def test_the_page_itself_needs_no_token_since_it_holds_no_data(self) -> None:
        self.assertEqual(self.call("GET", "/", token=None)[0], 200)

    def test_the_ticket_link_reaches_the_page(self) -> None:
        config = json.loads((self.dir / boards.CONFIG_FILE).read_text())
        (self.dir / boards.CONFIG_FILE).write_text(
            json.dumps({**config, "tickets": "https://linear.app/acme/issue/{id}"})
        )
        self.write_cards(card(ticket="SHOP-1"), card(id="no-ticket"))
        links = self.call("GET", "/api/board")[1]["ticketLinks"]
        self.assertEqual(links, {"SHOP-1": "https://linear.app/acme/issue/SHOP-1"})

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

    def test_a_ticket_is_set_shown_and_cleared(self) -> None:
        self.write_cards(card())
        self.assertEqual(self.cli("set", "shop-checkout", "--ticket", "SHOP-142")[0], 0)
        self.assertIn("SHOP-142", self.cli("ls")[1])
        self.cli("set", "shop-checkout", "--clear", "ticket")
        self.assertNotIn("ticket", boards.read(self.board).find("shop-checkout"))
        code, output = self.cli("set", "shop-checkout", "--ticket", "not a ticket")
        self.assertEqual(code, 1)
        self.assertIn("not a ticket ID", output)

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
        self.assertTrue(user.is_trusted(target))
        self.assertIn("server.log", (target / ".gitignore").read_text())
        self.assertEqual(cli.main(["--board", str(target), "init"]), 1)

    def test_init_above_several_repos_makes_a_project_for_each(self) -> None:
        self.repo("shop", "acme/shop", ["checkout"])
        self.repo("blog", "acme/blog")
        self.repo("other-blog", "someone/blog")
        target = self.root / "fresh" / boards.DIR_NAME
        target.parent.mkdir()
        for name in ("shop", "blog", "other-blog"):
            (self.root / name).rename(target.parent / name)
        with mock.patch.object(Path, "cwd", return_value=target.parent), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--board", str(target), "init", "--owner", "Ada"]), 0)
        projects = boards.load(target).projects
        self.assertEqual(
            {key: p.repo for key, p in projects.items()},
            {
                "shop": "acme/shop",
                "acme-blog": "acme/blog",
                "someone-blog": "someone/blog",
            },
        )

    def test_init_with_no_repo_anywhere_writes_nothing(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        with mock.patch.object(Path, "cwd", return_value=empty):
            code, output = self.cli_at(empty / boards.DIR_NAME, "init")
        self.assertEqual(code, 1)
        self.assertIn("No GitHub repo", output)
        self.assertFalse((empty / boards.DIR_NAME).exists())

    def cli_at(self, board: Path, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["--board", str(board), *args])
        return code, out.getvalue()

    def test_trust_lets_the_hooks_use_a_board(self) -> None:
        self.assertFalse(user.is_trusted(self.dir))
        self.assertEqual(self.cli("trust")[0], 0)
        self.assertTrue(user.is_trusted(self.dir))

    def test_the_hook_command_reads_its_payload_from_stdin(self) -> None:
        user.trust(self.dir)
        main = self.repo("shop", "acme/shop")
        payload = io.StringIO(json.dumps({"cwd": str(main), "session_id": "s"}))
        out = io.StringIO()
        with mock.patch("sys.stdin", payload), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["hook", "session-start"]), 0)
        self.assertIn("Ada's yourmove board", out.getvalue())


class Autostart(BoardCase):
    def setUp(self) -> None:
        super().setUp()
        # This copy of yourmove, as a plugin install keeps it: one folder per version.
        self.plugin = self.root / "plugin"
        self.source = self.plugin / "0.2.0" / "src"
        self.source.mkdir(parents=True)
        self.launchctl = mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", ""))
        for patcher in (
            mock.patch("sys.platform", "darwin"),
            mock.patch.object(autostart, "SOURCE", self.source),
            mock.patch("time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.run_mock = self.launchctl.start()
        self.addCleanup(self.launchctl.stop)

    def agent(self) -> dict[str, Any]:
        loaded: dict[str, Any] = plistlib.loads(autostart.agent_file().read_bytes())
        return loaded

    def write_agent(self, source: Path, arguments: list[str]) -> bytes:
        autostart.agent_file().parent.mkdir(parents=True)
        written = plistlib.dumps(
            {
                "ProgramArguments": ["python3", "-m", "yourmove", *arguments],
                "EnvironmentVariables": {"PYTHONPATH": str(source), "PATH": "/opt/bin", "XDG_CONFIG_HOME": "/x"},
            }
        )
        autostart.agent_file().write_bytes(written)
        return written

    def test_install_serves_the_board_on_its_port_with_this_config_folder(self) -> None:
        message = autostart.install(self.board, 47612)
        self.assertIn("http://127.0.0.1:47612/", message)
        self.assertIn("`yourmove open --port 47612`", message)
        agent = self.agent()
        self.assertEqual(agent["ProgramArguments"][-4:], [str(self.dir), "serve", "--port", "47612"])
        self.assertEqual(agent["EnvironmentVariables"]["XDG_CONFIG_HOME"], str(self.root / "config"))

    def test_a_server_whose_code_is_gone_moves_onto_this_copy_and_keeps_its_board_port_and_environment(self) -> None:
        self.write_agent(self.plugin / "0.1.0" / "src", ["--board", str(self.dir), "serve"])
        autostart.refresh()
        agent = self.agent()
        self.assertEqual(
            agent["EnvironmentVariables"], {"PYTHONPATH": str(self.source), "PATH": "/opt/bin", "XDG_CONFIG_HOME": "/x"}
        )
        self.assertEqual(agent["ProgramArguments"][-4:], [str(self.dir), "serve", "--port", str(boards.PORT)])

    def test_a_server_on_an_older_version_of_this_plugin_moves_up_before_that_version_is_deleted(self) -> None:
        older = self.plugin / "0.1.9" / "src"
        older.mkdir(parents=True)
        self.write_agent(older, ["--board", str(self.dir), "serve"])
        autostart.refresh()
        self.assertEqual(self.agent()["EnvironmentVariables"]["PYTHONPATH"], str(self.source))

    def test_a_server_on_a_newer_version_or_a_clone_is_left_alone(self) -> None:
        for other in (self.plugin / "0.10.0" / "src", self.root / "clone" / "src"):
            with self.subTest(other.parent.name):
                other.mkdir(parents=True)
                written = self.write_agent(other, ["--board", str(self.dir), "serve", "--port", "47613"])
                autostart.refresh()
                self.assertEqual(autostart.agent_file().read_bytes(), written)
                autostart.agent_file().unlink()
                autostart.agent_file().parent.rmdir()
        self.run_mock.assert_not_called()

    def test_a_login_server_this_code_did_not_write_is_never_rewritten(self) -> None:
        autostart.agent_file().parent.mkdir(parents=True)
        for unreadable in (b"not a plist", plistlib.dumps({"Label": "dev.yourmove.serve"})):
            with self.subTest(unreadable):
                autostart.agent_file().write_bytes(unreadable)
                autostart.refresh()
                self.assertEqual(autostart.agent_file().read_bytes(), unreadable)
        self.run_mock.assert_not_called()

    def test_a_bootstrap_that_keeps_failing_says_how_to_retry(self) -> None:
        self.run_mock.return_value = subprocess.CompletedProcess([], 5, "", "Bootstrap failed: 5: Input/output error")
        with self.assertRaisesRegex(boards.BoardError, "Input/output error.*yourmove autostart"):
            autostart.install(self.board, boards.PORT)
        bootstraps = [call for call in self.run_mock.call_args_list if call.args[0][1] == "bootstrap"]
        self.assertEqual(len(bootstraps), autostart.BOOTSTRAP_TRIES)

    def test_with_no_login_server_nothing_is_made(self) -> None:
        autostart.refresh()
        self.assertFalse(autostart.agent_file().exists())
