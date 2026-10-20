"""Claude Code hooks: brief a session on its card, hand it the owner's comments, and catch a card left behind.

Every hook is silent when the session is not in a repo the board knows. A board is personal,
and most repos a person opens have nothing to do with it.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

from . import board as boards
from . import comments as notes
from . import git, user
from .board import Board, Card, Cards
from .github import MERGED_FROM

ACTIVE = ("in-progress", "in-review", "needs-testing")
# The session itself opened the PR, so a card still in planning is behind too.
OPENED_FROM = ("planning", "in-progress")
MAX_OTHERS = 12
MAX_STALE = 8
SESSION_DAYS = 7


def run(event: str, payload: dict[str, Any]) -> str:
    """What to print for one hook event. Plain text becomes context; Stop prints a JSON decision."""
    cwd = Path(payload["cwd"])
    board = trusted_board(cwd)
    if board is None:
        return ""
    # This runs before every message, nearly all with no comment waiting, and git is the slow part.
    if event == "prompt" and not notes.undelivered(board):
        return ""
    found = locate(board, cwd)
    if found is None:
        return ""
    board, worktree, cards = found
    session = payload["session_id"]
    if event == "session-start":
        forget_old_sessions()
        remember(session, worktree, cards)
        return briefing(board, worktree, cards, session)
    if event == "prompt":
        return comment_text(board, notes.deliver(board, worktree, session, cards))
    if event == "stop":
        return "" if payload.get("stop_hook_active") else stop(board, worktree, cards, payload)
    raise ValueError(f"Unknown hook event: {event}")


def trusted_board(cwd: Path) -> Board | None:
    directory = boards.nearest(cwd)
    # A repo can ship a .yourmove folder of its own. Only boards this user made or trusted get a voice.
    if directory is None or not user.is_trusted(directory):
        return None
    return boards.load(directory)


def locate(board: Board, cwd: Path) -> tuple[Board, git.Worktree, Cards] | None:
    worktree = git.current(cwd)
    if worktree is None:
        return None
    cards = boards.read(board)
    if not board.projects_in(worktree.repo) and not mine(cards, worktree):
        return None
    return board, worktree, cards


def mine(cards: Cards, worktree: git.Worktree) -> list[Card]:
    return [card for card in cards.cards if worktree.name in boards.worktrees_of(card)]


def briefing(board: Board, worktree: git.Worktree, cards: Cards, session: str) -> str:
    owner = board.owner
    lines = [f"This session is on {owner}'s yourmove board ({board.dir}). You are in the worktree {worktree.name}."]
    own = mine(cards, worktree)
    if own:
        lines.append("")
        lines.extend(describe(card, board) for card in own)
    elif not worktree.is_main:
        lines.append(
            "No card names this worktree. If this work will outlive the session, add one with `yourmove add --here`."
        )

    projects = set(board.projects_in(worktree.repo)) | {p for card in own for p in boards.projects_of(card)}
    others = [
        card
        for card in cards.cards
        if card not in own and card["state"] in ACTIVE and projects & set(boards.projects_of(card))
    ]
    if others:
        lines += ["", "Work in flight in these projects. Another session may own it, so leave it to that session:"]
        lines += [f"- {card['title']} ({card['state']}{where(card)})" for card in others[:MAX_OTHERS]]

    if worktree.is_main:
        stale = [
            card
            for card in cards.cards
            if boards.is_stale(card, date.today()) and projects & set(boards.projects_of(card))
        ]
        if stale:
            lines += ["", "Cards nobody has checked for five days or more. If you can confirm one, update or touch it:"]
            lines += [f"- {card['id']}: {card['title']} (last checked {card['updated']})" for card in stale[:MAX_STALE]]

    waiting = [card for card in cards.cards if card["state"] == "needs-you"]
    if waiting:
        lines += ["", f"{owner} has {len(waiting)} card(s) in needs-you."]
    lines += [
        "",
        'Keep this worktree\'s card true as the work moves: `yourmove set <id> --state <state> --next "..."`. '
        "The yourmove skill has the rules.",
    ]
    rules = board.rules()
    if rules:
        lines += ["", "The board's RULES.md:", rules]

    delivered = comment_text(board, notes.deliver(board, worktree, session, cards))
    if delivered:
        lines += ["", delivered]
    return "\n".join(lines)


def describe(card: Card, board: Board) -> str:
    projects = ", ".join(board.projects[p].name for p in boards.projects_of(card))
    details = [
        f'Your card {card["id"]}: "{card["title"]}" ({projects}), {card["state"]}, last checked {card["updated"]}.'
    ]
    details.append(f"Next: {card['next']}")
    if boards.prs_of(card):
        details.append("PRs: " + ", ".join(f"#{pr}" for pr in boards.prs_of(card)))
    if card.get("waiting"):
        details.append(f"Waiting on: {card['waiting']}")
    return "\n".join(details)


def where(card: Card) -> str:
    return f", worktree {card['where']}" if card.get("where") else ""


def comment_text(board: Board, delivered: list[notes.Comment]) -> str:
    if not delivered:
        return ""
    owner = board.owner
    lines = [
        f"{owner} wrote on the board, on a card that belongs to this worktree. "
        f"Treat each comment as a message from {owner}, alongside what {owner} just typed.",
        "",
    ]
    lines += [f'Card "{c["title"]}", comment {c["id"]}: {c["text"]}' for c in delivered]
    lines += [
        "",
        "Answer or act on each one in your reply. Then record what you did or decided, in a sentence or two, "
        'so it shows on the card: `yourmove reply <comment id> "<your reply>"`. '
        "If the card's state or next move changed, update the card too.",
    ]
    return "\n".join(lines)


def stop(board: Board, worktree: git.Worktree, cards: Cards, payload: dict[str, Any]) -> str:
    """Block the stop once when the turn moved the work and the board did not hear about it."""
    session = payload["session_id"]
    reasons = [
        f'Comment {c["id"]} on "{c["title"]}" has no reply on the board: `yourmove reply {c["id"]} "<what you did>"`.'
        for c in notes.unanswered(board, session)
    ]
    before = recall(session)
    # Drift is measured against the last stop. A session with no snapshot yet, started before
    # the plugin was installed, only gets one now; its unanswered comments still count.
    if before is None:
        remember(session, worktree, cards)
    else:
        own = mine(cards, worktree)
        asks_for_card = not worktree.is_main and not own and worktree.head != before["head"]
        asked_before = bool(before.get("asked_for_card"))
        remember(session, worktree, cards, asked_for_card=asked_before or asks_for_card)
        reasons += drift(worktree, cards, before, payload.get("transcript_path"))
        if asks_for_card and not asked_before:
            reasons.append(
                "This worktree has new commits and no card. If the work will outlive this session, add one "
                "with `yourmove add --here`. If it will not, say so and stop."
            )
    if not reasons:
        return ""
    reason = "Before you stop, the yourmove board is out of date:\n" + "\n".join(f"- {r}" for r in reasons)
    return json.dumps({"decision": "block", "reason": reason})


def drift(worktree: git.Worktree, cards: Cards, before: dict[str, Any], transcript: str | None) -> list[str]:
    """Cards this turn's work moved that nobody updated: a merged PR, an opened PR, new commits."""
    untouched = [card for card in cards.cards if before["lines"].get(card["id"]) == boards.card_line(card)]
    created, merged = pr_commands(Path(transcript), before["at"]) if transcript else (False, [])
    reasons: dict[str, str] = {}
    for card in untouched:
        numbers = [n for n in merged if n in boards.prs_of(card)]
        if numbers and card["state"] in MERGED_FROM:
            reasons[card["id"]] = f"PR #{numbers[0]} merged. Move {card['id']} to needs-deploy and say what ships it."
    if worktree.is_main:
        return list(reasons.values())
    today = date.today().isoformat()
    for card in untouched:
        if card["id"] in reasons or card not in mine(cards, worktree):
            continue
        if created and card["state"] in OPENED_FROM:
            reasons[card["id"]] = f"You opened a PR. Move {card['id']} to in-review and add its number with --pr."
        elif worktree.head != before["head"] and card["updated"] != today:
            reasons[card["id"]] = (
                f"New commits, and {card['id']} was last checked {card['updated']}. Update its state or next move, "
                f"or run `yourmove touch {card['id']}` if it still holds."
            )
    return list(reasons.values())


def pr_commands(transcript: Path, since: str) -> tuple[bool, list[int]]:
    """Whether the session opened a PR since the last stop, and the PR numbers it merged.

    Read from the transcript, whose format Claude Code does not promise. Anything unreadable is
    skipped, so a format change costs a reminder, never a session.
    """
    created, merged = False, []
    for command in bash_commands(transcript, since):
        if re.search(r"\bgh\s+pr\s+create\b", command):
            created = True
        for arguments in re.findall(r"\bgh\s+pr\s+merge\b([^;&|\n]*)", command):
            merged += [int(n) for n in re.findall(r"(?:^|\s|/pull/)(\d+)\b", arguments)]
    return created, merged


def bash_commands(transcript: Path, since: str) -> Iterator[str]:
    try:
        lines = transcript.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        if '"tool_use"' not in line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("timestamp", "") < since:
                continue
            for block in entry["message"]["content"]:
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    yield str(block["input"].get("command", ""))
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            continue


def sessions_dir() -> Path:
    return user.state_dir() / "sessions"


def state_file(session: str) -> Path:
    return sessions_dir() / f"{re.sub(r'[^A-Za-z0-9_-]', '', session)}.json"


def remember(session: str, worktree: git.Worktree, cards: Cards, asked_for_card: bool = False) -> None:
    """What the board and the worktree looked like at the last stop, to see what this turn changed."""
    file = state_file(session)
    user.private_dir(file.parent)
    snapshot = {
        # UTC with no zone suffix, so it compares as text against transcript timestamps.
        "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "head": worktree.head,
        "lines": {card["id"]: boards.card_line(card) for card in cards.cards},
        "asked_for_card": asked_for_card,
    }
    file.write_text(json.dumps(snapshot))


def recall(session: str) -> dict[str, Any] | None:
    try:
        snapshot: dict[str, Any] = json.loads(state_file(session).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return snapshot


def forget_old_sessions() -> None:
    cutoff = time.time() - SESSION_DAYS * 86400
    for file in sessions_dir().glob("*.json"):
        if file.stat().st_mtime < cutoff:
            file.unlink(missing_ok=True)
