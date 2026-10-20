"""The board directory: its config, its cards, and the lock every writer takes."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

# A card names its next move, so the column is the move: whose it is and what kind.
STATES = (
    "needs-you",
    "planning",
    "in-progress",
    "in-review",
    "needs-testing",
    "needs-deploy",
    "live",
)
STALE_AFTER_DAYS = 5
# Two short sentences. Detail belongs in the PR or the worktree, where it can be kept current.
NEXT_LIMIT = 280
FIELDS = ("id", "project", "state", "title", "next", "updated", "pr", "where", "waiting", "choices")

DIR_NAME = ".yourmove"
CONFIG_FILE = "yourmove.json"
CARDS_FILE = "cards.jsonl"
COMMENTS_FILE = "comments.jsonl"
HISTORY_FILE = "history.jsonl"
RULES_FILE = "RULES.md"
LOCK_WAIT_SECONDS = 5
STALE_LOCK_SECONDS = 30

Card = dict[str, Any]


class BoardError(Exception):
    """Something the person has to fix: a missing board, a bad config, an unknown card."""


@dataclass(frozen=True)
class Project:
    name: str
    color: str
    repo: str  # "owner/name", the GitHub repo its PRs and worktrees belong to


@dataclass(frozen=True)
class Board:
    dir: Path
    owner: str
    projects: dict[str, Project]

    def path(self, name: str) -> Path:
        return self.dir / name

    def projects_in(self, repo: str) -> list[str]:
        return [key for key, project in self.projects.items() if project.repo.lower() == repo.lower()]

    def rules(self) -> str:
        rules = self.path(RULES_FILE)
        return rules.read_text().strip() if rules.exists() else ""


@dataclass(frozen=True)
class Problem:
    line: int
    message: str


@dataclass(frozen=True)
class Cards:
    """Every line of cards.jsonl that parses into a valid card, and what is wrong with the rest."""

    cards: list[Card]
    errors: list[Problem]

    def find(self, card_id: str) -> Card:
        for card in self.cards:
            if card["id"] == card_id:
                return card
        raise BoardError(f"No card with id {card_id}. Run `yourmove ls` to see them.")


def find(start: Path) -> Board:
    directory = nearest(start)
    if directory is None:
        raise BoardError(f"No board found at or above {start}. Run `yourmove init` to make one.")
    return load(directory)


def nearest(start: Path) -> Path | None:
    """The nearest .yourmove directory at or above start, the way git finds .git."""
    for directory in [start, *start.parents]:
        if (directory / DIR_NAME / CONFIG_FILE).exists():
            return directory / DIR_NAME
    return None


def load(directory: Path) -> Board:
    config_file = directory / CONFIG_FILE
    try:
        config = json.loads(config_file.read_text())
    except FileNotFoundError:
        raise BoardError(f"{directory} has no {CONFIG_FILE}. Run `yourmove init` there.") from None
    except json.JSONDecodeError as error:
        raise BoardError(f"{config_file} is not valid JSON: {error}") from None

    owner = config.get("owner") if isinstance(config, dict) else None
    projects = config.get("projects") if isinstance(config, dict) else None
    if not isinstance(owner, str) or not owner.strip():
        raise BoardError(f'{config_file} needs "owner": the name of the person the board belongs to.')
    if not isinstance(projects, dict) or not projects:
        raise BoardError(f'{config_file} needs "projects", with at least one project.')
    parsed = {}
    for key, project in projects.items():
        if not isinstance(project, dict) or not all(
            isinstance(project.get(field), str) and project[field] for field in ("name", "color", "repo")
        ):
            raise BoardError(f'Project "{key}" in {config_file} needs a "name", a "color" and a "repo" (owner/name).')
        parsed[key] = Project(project["name"], project["color"], project["repo"])
    return Board(directory, owner.strip(), parsed)


def projects_of(card: Card) -> list[str]:
    project = card.get("project")
    if project is None:
        return []
    return project if isinstance(project, list) else [project]


def prs_of(card: Card) -> list[int]:
    pr = card.get("pr")
    if pr is None:
        return []
    return pr if isinstance(pr, list) else [pr]


def worktrees_of(card: Card) -> list[str]:
    return [name.strip() for name in card.get("where", "").split(",") if name.strip()]


def is_stale(card: Card, today: date) -> bool:
    return card["state"] != "live" and (today - date.fromisoformat(card["updated"])).days >= STALE_AFTER_DAYS


def errors_in(card: Any, board: Board) -> list[str]:
    """Why a card cannot be shown. Cards are written by hand and by agents, so trust nothing."""
    if not isinstance(card, dict):
        return ["is not a JSON object"]
    errors = []
    for field in ("id", "title", "next"):
        if not isinstance(card.get(field), str) or not card[field].strip():
            errors.append(f'needs a "{field}"')
    if isinstance(card.get("id"), str) and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", card["id"]):
        errors.append(f"has an id that is not lowercase-with-hyphens: {card['id']}")
    if card.get("state") not in STATES:
        errors.append(f"has an unknown state: {card.get('state')}. Use one of {', '.join(STATES)}")
    projects = projects_of(card)
    if not projects or any(not isinstance(p, str) or p not in board.projects for p in projects):
        errors.append(f"has an unknown project: {card.get('project')}. Use one of {', '.join(board.projects)}")
    try:
        date.fromisoformat(card.get("updated", ""))
    except (TypeError, ValueError):
        errors.append('needs an "updated" date like 2026-10-13')
    if not all(isinstance(pr, int) and not isinstance(pr, bool) and pr > 0 for pr in prs_of(card)):
        errors.append('has a "pr" that is not a PR number or a list of them')
    for field in ("where", "waiting"):
        if field in card and not isinstance(card[field], str):
            errors.append(f'has a "{field}" that is not text')
    choices = card.get("choices", [])
    if not isinstance(choices, list) or not all(isinstance(c, str) and c.strip() for c in choices):
        errors.append('has "choices" that are not a list of replies')
    return errors


def warnings_in(card: Card, today: date) -> list[str]:
    """What makes a valid card less useful. Shown by lint, never blocks the card."""
    warnings = []
    if len(card["next"]) > NEXT_LIMIT:
        warnings.append(
            f'has a "next" of {len(card["next"])} characters. Keep it under {NEXT_LIMIT}: '
            "the move, not the history. Detail goes in the PR."
        )
    if card.get("choices") and card["state"] != "needs-you":
        warnings.append('has "choices" but is not in needs-you, so nobody is asked to pick one')
    if is_stale(card, today):
        warnings.append(f"was last checked {card['updated']}. Check it still holds and update it")
    return warnings


def parse(lines: list[str], board: Board) -> Cards:
    cards: list[Card] = []
    errors: list[Problem] = []
    seen = set()
    for number, text in enumerate(lines, start=1):
        if not text.strip():
            continue
        try:
            card = json.loads(text)
        except json.JSONDecodeError as error:
            errors.append(Problem(number, f"Line {number} is not valid JSON: {error.msg}"))
            continue
        label = (isinstance(card, dict) and (card.get("title") or card.get("id"))) or f"Line {number}"
        problems = errors_in(card, board)
        if not problems and card["id"] in seen:
            problems = [f"reuses the id {card['id']}"]
        if problems:
            errors.extend(Problem(number, f'"{label}" (line {number}) {problem}') for problem in problems)
            continue
        seen.add(card["id"])
        cards.append(card)
    return Cards(cards, errors)


def read(board: Board) -> Cards:
    return parse(read_lines(board.path(CARDS_FILE)), board)


def read_lines(file: Path) -> list[str]:
    return file.read_text().splitlines() if file.exists() else []


def write_lines(file: Path, lines: list[str]) -> None:
    # Written beside the file and renamed over it, so a reader never sees half a file.
    temp = file.with_name(f".{file.name}.tmp")
    temp.write_text("".join(f"{line}\n" for line in lines))
    os.replace(temp, file)


def card_line(card: Card) -> str:
    ordered = {field: card[field] for field in FIELDS if field in card}
    ordered.update((field, value) for field, value in card.items() if field not in ordered)
    return json.dumps(ordered, ensure_ascii=False)


@contextmanager
def locked(board: Board) -> Iterator[None]:
    """Held by every read-modify-write, because many sessions edit the same board at once.

    A directory, because creating one is atomic on every platform. A lock older than
    STALE_LOCK_SECONDS belongs to a process that died holding it.
    """
    lock = board.path(".lock")
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                    lock.rmdir()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise BoardError(
                    f"The board is locked by another writer. If nothing is writing, remove {lock}."
                ) from None
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.rmdir()


def change(board: Board, edit: Callable[[list[str], Cards], list[str]]) -> None:
    """Rewrite cards.jsonl under the lock. Lines the edit does not touch keep their exact text."""
    file = board.path(CARDS_FILE)
    with locked(board):
        lines = read_lines(file)
        write_lines(file, edit(lines, parse(lines, board)))


def line_of(lines: list[str], card_id: str) -> int:
    for index, text in enumerate(lines):
        try:
            card = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(card, dict) and card.get("id") == card_id:
            return index
    raise BoardError(f"No card with id {card_id}. Run `yourmove ls` to see them.")


def checked(card: Card, board: Board) -> Card:
    errors = errors_in(card, board)
    if errors:
        raise BoardError(f"The card {'; '.join(errors)}.")
    if len(card["next"]) > NEXT_LIMIT:
        raise BoardError(
            f'"next" is {len(card["next"])} characters; the limit is {NEXT_LIMIT}. Write the next move in one or '
            "two short sentences. History and detail go in the PR description or a comment on it."
        )
    return card


def new_id(title: str, project: str, taken: set[str]) -> str:
    words = re.findall(r"[a-z0-9]+", f"{project} {title}".lower())[:6]
    base = "-".join(words)[:48].strip("-") or project
    candidate, suffix = base, 2
    while candidate in taken:
        candidate, suffix = f"{base}-{suffix}", suffix + 1
    return candidate


def add(board: Board, card: Card, first: bool = False) -> Card:
    """Add a card to its column: last by default, since order inside a column is priority."""
    added: Card = {}

    def edit(lines: list[str], current: Cards) -> list[str]:
        added.update(card)
        added.setdefault("id", new_id(card["title"], projects_of(card)[0], {c["id"] for c in current.cards}))
        added["updated"] = date.today().isoformat()
        checked(added, board)
        if any(c["id"] == added["id"] for c in current.cards):
            raise BoardError(f"A card with id {added['id']} already exists.")
        same_state = [line_of(lines, c["id"]) for c in current.cards if c["state"] == added["state"]]
        if not same_state:
            return [*lines, card_line(added)]
        at = same_state[0] if first else same_state[-1] + 1
        return [*lines[:at], card_line(added), *lines[at:]]

    change(board, edit)
    record(board, {"event": "add", "card": added["id"], "title": added["title"], "to": added["state"]})
    return added


def update(board: Board, card_id: str, fields: dict[str, Any], by: str) -> Card:
    """Change a card and mark it checked today. A None value removes that field."""
    before: Card = {}
    after: Card = {}

    def edit(lines: list[str], current: Cards) -> list[str]:
        index = line_of(lines, card_id)
        before.update(current.find(card_id))
        after.update(before)
        for field, value in fields.items():
            if value is None:
                after.pop(field, None)
            else:
                after[field] = value
        after["updated"] = date.today().isoformat()
        checked(after, board)
        return [*lines[:index], card_line(after), *lines[index + 1 :]]

    change(board, edit)
    if before["state"] != after["state"]:
        record(
            board,
            {
                "event": "move",
                "card": card_id,
                "title": after["title"],
                "from": before["state"],
                "to": after["state"],
                "by": by,
            },
        )
    return after


def remove(board: Board, card_id: str, by: str) -> Card:
    removed: Card = {}

    def edit(lines: list[str], current: Cards) -> list[str]:
        index = line_of(lines, card_id)
        removed.update(json.loads(lines[index]))
        return [*lines[:index], *lines[index + 1 :]]

    change(board, edit)
    record(board, {"event": "remove", "card": card_id, "title": removed.get("title", card_id), "by": by})
    return removed


def record(board: Board, entry: dict[str, Any]) -> None:
    """Append to the history, which answers "what moved this week" without anyone keeping notes."""
    line = json.dumps({"at": now(), **entry}, ensure_ascii=False)
    with board.path(HISTORY_FILE).open("a") as history:
        history.write(f"{line}\n")


def history(board: Board, since: date | None = None) -> list[dict[str, Any]]:
    entries = []
    for text in read_lines(board.path(HISTORY_FILE)):
        try:
            entry = json.loads(text)
        except json.JSONDecodeError:
            continue
        if since is None or entry["at"][:10] >= since.isoformat():
            entries.append(entry)
    return entries


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
