"""Comments the owner writes on cards, and the replies sessions record. One per line in comments.jsonl.

A comment reaches a session as the owner's own words, so only the board page may write one.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from . import board as boards
from . import user
from .board import Board, BoardError, Card, Cards
from .git import Worktree

Comment = dict[str, Any]
MAX_TEXT = 2000


def load(board: Board) -> list[Comment]:
    """The comments this machine's board server signed. Anything else in the file is dropped."""
    comments = []
    for text in boards.read_lines(board.path(boards.COMMENTS_FILE)):
        try:
            comment = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(comment, dict) and user.is_signed(comment, board.dir):
            comments.append(comment)
    return comments


def save(board: Board, comments: list[Comment]) -> None:
    boards.write_lines(board.path(boards.COMMENTS_FILE), [json.dumps(c, ensure_ascii=False) for c in comments])


def add(board: Board, card: Card, text: str) -> list[Comment]:
    text = text.strip()
    if not 0 < len(text) <= MAX_TEXT:
        raise BoardError(f"A comment needs between 1 and {MAX_TEXT} characters.")
    comment = {
        "id": secrets.token_hex(4),
        "card": card["id"],
        # Kept so the comment can still be shown and routed after its card is renamed or removed.
        "title": card["title"],
        "where": card.get("where", ""),
        "project": boards.projects_of(card),
        "text": text,
        "at": boards.now(),
    }
    comment["sig"] = user.signature(comment, board.dir)
    with boards.locked(board):
        comments = [*load(board), comment]
        save(board, comments)
    return comments


def remove(board: Board, comment_id: str) -> list[Comment]:
    with boards.locked(board):
        comments = [c for c in load(board) if c["id"] != comment_id]
        save(board, comments)
    return comments


def reply(board: Board, comment_id: str, text: str) -> Comment:
    text = text.strip()
    if not text:
        raise BoardError("The reply is empty. Nothing saved.")
    with boards.locked(board):
        comments = load(board)
        matching = [c for c in comments if c["id"] == comment_id]
        if not matching:
            raise BoardError(f"No comment {comment_id}. Nothing saved.")
        matching[0]["reply"] = {"text": text, "at": boards.now()}
        save(board, comments)
    return matching[0]


def is_addressed_to(comment: Comment, worktree: Worktree, cards: Cards, board: Board) -> bool:
    """The session in the card's worktree. With that worktree gone, the next session in its repo's main one."""
    card = next((c for c in cards.cards if c["id"] == comment["card"]), None)
    named = boards.worktrees_of(card) if card else boards.worktrees_of(comment)
    projects = boards.projects_of(card) if card else comment["project"]
    live = [name for name in named if name in worktree.siblings]
    if live:
        return worktree.name in live
    owned_here = set(projects) & set(board.projects_in(worktree.repo))
    return worktree.is_main and bool(owned_here)


def deliver(board: Board, worktree: Worktree, session_id: str, cards: Cards) -> list[Comment]:
    """The comments no session has been handed yet, recorded as handed to this one."""

    def waiting(comment: Comment, delivered: dict[str, str]) -> bool:
        return comment["sig"] not in delivered and is_addressed_to(comment, worktree, cards, board)

    # Runs on every prompt, so look before taking the lock other sessions are waiting on.
    if not any(waiting(c, user.deliveries()) for c in load(board)):
        return []
    with boards.locked(board):
        comments = load(board)
        delivered = user.deliveries()
        unseen = [c for c in comments if waiting(c, delivered)]
        user.record_deliveries([c["sig"] for c in unseen], session_id)
        # Only for the page to show. Whether a comment was delivered is decided by the private record.
        for comment in unseen:
            comment["seen"] = {"at": boards.now(), "worktree": worktree.name, "session": session_id}
        if unseen:
            save(board, comments)
    return unseen


def unanswered(board: Board, session_id: str) -> list[Comment]:
    delivered = user.deliveries()
    return [c for c in load(board) if delivered.get(c["sig"]) == session_id and "reply" not in c]
