from __future__ import annotations

import json
import os
import threading
import time
from datetime import date

from support import LONG_AGO, TODAY, BoardCase, card
from yourmove import board as boards
from yourmove.board import BoardError


class ReadingCards(BoardCase):
    def test_a_valid_card_is_read(self) -> None:
        self.write_cards(card())
        cards = boards.read(self.board)
        self.assertEqual([c["id"] for c in cards.cards], ["shop-checkout"])
        self.assertEqual(cards.errors, [])

    def test_a_missing_cards_file_is_an_empty_board(self) -> None:
        self.assertEqual(boards.read(self.board).cards, [])

    def test_a_bad_line_is_named_and_the_rest_still_show(self) -> None:
        self.write_lines(["{not json", boards.card_line(card(id="ok"))])
        cards = boards.read(self.board)
        self.assertEqual([c["id"] for c in cards.cards], ["ok"])
        self.assertIn("Line 1 is not valid JSON", cards.errors[0].message)

    def test_each_kind_of_bad_card_says_what_is_wrong(self) -> None:
        cases = {
            "unknown state": card(state="done"),
            "unknown project": card(project="nope"),
            '"updated" date': card(updated="Oct 13"),
            '"pr"': card(pr=True),
            "lowercase-with-hyphens": card(id="Shop Checkout"),
            '"choices"': card(choices="yes"),
            'needs a "next"': card(next="  "),
            "not a ticket ID": card(ticket="SHOP 142"),
        }
        for expected, bad in cases.items():
            with self.subTest(expected):
                self.write_cards(bad)
                cards = boards.read(self.board)
                self.assertEqual(cards.cards, [])
                self.assertIn(expected, cards.errors[0].message)

    def test_the_second_card_with_an_id_is_refused(self) -> None:
        self.write_cards(card(title="First"), card(title="Second"))
        cards = boards.read(self.board)
        self.assertEqual([c["title"] for c in cards.cards], ["First"])
        self.assertIn("reuses the id", cards.errors[0].message)

    def test_a_card_can_span_projects_and_prs(self) -> None:
        self.write_cards(card(project=["shop", "blog"], pr=[1, 2]))
        self.assertEqual(boards.read(self.board).errors, [])


class Warnings(BoardCase):
    def test_a_long_next_a_stale_card_and_stray_choices_are_warned_about(self) -> None:
        warned = boards.warnings_in(card(next="x" * 300, updated=LONG_AGO, choices=["Yes"]), date.today())
        self.assertEqual(len(warned), 3)

    def test_a_live_card_is_never_stale(self) -> None:
        self.assertEqual(boards.warnings_in(card(state="live", updated=LONG_AGO), date.today()), [])


class FindingTheBoard(BoardCase):
    def test_the_nearest_board_above_is_found(self) -> None:
        deep = self.root / "a" / "b"
        deep.mkdir(parents=True)
        self.assertEqual(boards.find(deep).dir, self.dir)

    def test_no_board_says_how_to_make_one(self) -> None:
        with self.assertRaisesRegex(BoardError, "yourmove init"):
            boards.find(self.root.parent)

    def test_a_tickets_link_needs_https_and_a_place_for_the_id(self) -> None:
        config = json.loads((self.dir / boards.CONFIG_FILE).read_text())
        for bad in ("https://linear.app/acme/issue/", "javascript:alert('{id}')", 7):
            with self.subTest(bad):
                (self.dir / boards.CONFIG_FILE).write_text(json.dumps({**config, "tickets": bad}))
                with self.assertRaisesRegex(BoardError, "tickets"):
                    boards.load(self.dir)
        (self.dir / boards.CONFIG_FILE).write_text(
            json.dumps({**config, "tickets": "https://linear.app/acme/issue/{id}"})
        )
        self.assertEqual(boards.load(self.dir).ticket_link("SHOP-1"), "https://linear.app/acme/issue/SHOP-1")

    def test_a_config_without_projects_is_refused(self) -> None:
        (self.dir / boards.CONFIG_FILE).write_text(json.dumps({"owner": "Ada", "projects": {}}))
        with self.assertRaisesRegex(BoardError, "projects"):
            boards.load(self.dir)

    def test_a_project_without_a_repo_is_refused(self) -> None:
        (self.dir / boards.CONFIG_FILE).write_text(json.dumps({"owner": "Ada", "projects": {"x": {"name": "X"}}}))
        with self.assertRaisesRegex(BoardError, "repo"):
            boards.load(self.dir)


class AddingCards(BoardCase):
    def test_a_new_card_gets_an_id_from_its_project_and_title(self) -> None:
        added = boards.add(
            self.board, {"project": "shop", "state": "planning", "title": "Gift cards!", "next": "Spec."}
        )
        self.assertEqual(added["id"], "shop-gift-cards")
        self.assertEqual(added["updated"], TODAY)

    def test_a_clashing_id_gets_a_number(self) -> None:
        fields = {"project": "shop", "state": "planning", "title": "Gift cards", "next": "Spec."}
        boards.add(self.board, dict(fields))
        self.assertEqual(boards.add(self.board, dict(fields))["id"], "shop-gift-cards-2")

    def test_a_card_goes_last_in_its_column_or_first_when_asked(self) -> None:
        self.write_cards(card(id="a", state="planning"), card(id="b", state="live"), card(id="c", state="planning"))
        boards.add(self.board, {"id": "last", "project": "shop", "state": "planning", "title": "L", "next": "N"})
        boards.add(
            self.board, {"id": "top", "project": "shop", "state": "planning", "title": "T", "next": "N"}, first=True
        )
        order = [c["id"] for c in boards.read(self.board).cards]
        self.assertEqual(order, ["top", "a", "b", "c", "last"])

    def test_an_overlong_next_is_refused_and_nothing_is_written(self) -> None:
        with self.assertRaisesRegex(BoardError, "limit is 280"):
            boards.add(self.board, {"project": "shop", "state": "planning", "title": "T", "next": "x" * 281})
        self.assertEqual(boards.read(self.board).cards, [])

    def test_adding_is_recorded_in_the_history(self) -> None:
        boards.add(self.board, {"project": "shop", "state": "planning", "title": "T", "next": "N"})
        self.assertEqual(boards.history(self.board)[0]["event"], "add")

    def test_concurrent_writers_never_lose_a_card(self) -> None:
        def add(n: int) -> None:
            boards.add(self.board, {"project": "shop", "state": "planning", "title": f"Card {n}", "next": "N"})

        threads = [threading.Thread(target=add, args=(n,)) for n in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(boards.read(self.board).cards), 12)


class UpdatingCards(BoardCase):
    def test_only_the_changed_line_is_rewritten(self) -> None:
        hand_written = (
            '{"title": "By hand", "id": "hand", "project": "blog", "state": "live", '
            '"next": "Watch.", "updated": "2026-01-01"}'
        )
        self.write_lines([hand_written, boards.card_line(card())])
        boards.update(self.board, "shop-checkout", {"state": "in-review", "pr": 12}, by="test")
        lines = self.lines()
        self.assertEqual(lines[0], hand_written)
        self.assertEqual(json.loads(lines[1])["state"], "in-review")

    def test_an_update_marks_the_card_checked_today_and_none_removes_a_field(self) -> None:
        self.write_cards(card(updated=LONG_AGO, waiting="Sam"))
        updated = boards.update(self.board, "shop-checkout", {"waiting": None}, by="test")
        self.assertEqual(updated["updated"], TODAY)
        self.assertNotIn("waiting", updated)

    def test_a_move_is_recorded_with_who_made_it(self) -> None:
        self.write_cards(card())
        boards.update(self.board, "shop-checkout", {"state": "needs-deploy"}, by="checkout-tree")
        entry = boards.history(self.board)[-1]
        self.assertEqual((entry["from"], entry["to"], entry["by"]), ("in-progress", "needs-deploy", "checkout-tree"))

    def test_a_bad_update_leaves_the_file_as_it_was(self) -> None:
        self.write_cards(card())
        before = self.lines()
        with self.assertRaises(BoardError):
            boards.update(self.board, "shop-checkout", {"state": "done"}, by="test")
        self.assertEqual(self.lines(), before)

    def test_an_unknown_id_says_how_to_find_one(self) -> None:
        self.write_cards(card())
        with self.assertRaisesRegex(BoardError, "yourmove ls"):
            boards.update(self.board, "nope", {}, by="test")

    def test_removing_a_card_drops_its_line(self) -> None:
        self.write_cards(card(id="a"), card(id="b"))
        boards.remove(self.board, "a", by="test")
        self.assertEqual([c["id"] for c in boards.read(self.board).cards], ["b"])


class Locking(BoardCase):
    def test_a_lock_left_by_a_dead_writer_is_broken(self) -> None:
        lock = self.dir / ".lock"
        lock.mkdir()
        old = time.time() - boards.STALE_LOCK_SECONDS - 1
        os.utime(lock, (old, old))
        with boards.locked(self.board):
            pass
        self.assertFalse(lock.exists())
