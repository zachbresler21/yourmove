"""Moves the cards whose PRs moved, so the mechanical states never wait on a session remembering.

Only unambiguous moves are made. A card waiting on the owner stays in needs-you, and a PR
closed without merging is reported, never acted on: only a person knows whether the work died.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from . import board as boards
from .board import Board, Card

# A planning card can list PRs it means to rebuild or follow up on, so only work under way moves.
OPENED_FROM = {"in-progress"}
MERGED_FROM = {"in-progress", "in-review", "needs-testing"}
RECENT_PRS = 300


@dataclass(frozen=True)
class Move:
    card: Card
    to: str
    why: str


def available() -> bool:
    return shutil.which("gh") is not None


def plan(board: Board) -> tuple[list[Move], list[str]]:
    """The moves to make, and what needs a person: closed PRs and PRs gh could not read."""
    cards = [c for c in boards.read(board).cards if boards.prs_of(c)]
    states = pr_states(board, cards)
    moves, notes = [], []
    for card in cards:
        repo = board.projects[boards.projects_of(card)[0]].repo
        found = [states.get((repo, number)) for number in boards.prs_of(card)]
        if None in found:
            notes.append(f'"{card["title"]}": could not read PR state from {repo}.')
            continue
        if "CLOSED" in found:
            notes.append(f'"{card["title"]}": a PR was closed without merging. Decide whether the card should go.')
        elif all(state == "MERGED" for state in found) and card["state"] in MERGED_FROM:
            moves.append(Move(card, "needs-deploy", "its PR merged"))
        elif "OPEN" in found and card["state"] in OPENED_FROM:
            moves.append(Move(card, "in-review", "its PR is open"))
    return moves, notes


def apply(board: Board, moves: list[Move]) -> list[Move]:
    """Make the moves whose card has not changed state since the plan. Returns the ones made."""
    made: list[Move] = []

    # The state moves but "updated" stays: sync knows the PR moved, not that "next" still holds.
    def edit(lines: list[str], current: boards.Cards) -> list[str]:
        for move in moves:
            card = next((c for c in current.cards if c["id"] == move.card["id"]), None)
            if card is None or card["state"] != move.card["state"]:
                continue
            lines[boards.line_of(lines, card["id"])] = boards.card_line({**card, "state": move.to})
            made.append(move)
        return lines

    boards.change(board, edit)
    for move in made:
        boards.record(
            board,
            {
                "event": "move",
                "card": move.card["id"],
                "title": move.card["title"],
                "from": move.card["state"],
                "to": move.to,
                "by": "sync",
            },
        )
    return made


def pr_states(board: Board, cards: list[Card]) -> dict[tuple[str, int], str]:
    """OPEN, MERGED or CLOSED for every PR on a card. One list call per repo, then one call per straggler."""
    wanted = {
        (board.projects[boards.projects_of(card)[0]].repo, number) for card in cards for number in boards.prs_of(card)
    }
    states: dict[tuple[str, int], str] = {}
    # A repo gh cannot list (signed out, offline, no access) would fail every view call too.
    unreachable = set()
    for repo in {repo for repo, _ in wanted}:
        listed = gh(
            "pr", "list", "--repo", repo, "--state", "all", "--limit", str(RECENT_PRS), "--json", "number,state"
        )
        if listed is None:
            unreachable.add(repo)
            continue
        for pr in json.loads(listed):
            states[(repo, pr["number"])] = pr["state"]
    for repo, number in wanted - states.keys():
        if repo in unreachable:
            continue
        viewed = gh("pr", "view", str(number), "--repo", repo, "--json", "state")
        if viewed:
            states[(repo, number)] = json.loads(viewed)["state"]
    return states


def gh(*args: str) -> str | None:
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None
