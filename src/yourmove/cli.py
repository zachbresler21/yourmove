"""The yourmove command. Agents keep the board with it; people mostly read the page."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import webbrowser
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import board as boards
from . import comments as notes
from . import git, github, hooks, user
from .board import PORT, STATES, Board, BoardError, Card


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.run(args)
    except BoardError as error:
        print(error, file=sys.stderr)
        return 1
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yourmove", description="A board your coding agents keep for you.")
    parser.add_argument("--board", type=Path, help="the .yourmove directory (default: the nearest one above here)")
    commands = parser.add_subparsers(required=True, metavar="command")

    def command(name: str, run: Any, summary: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=summary, description=summary)
        sub.set_defaults(run=run)
        return sub

    init = command("init", run_init, "Make a board in .yourmove/ here, with this repo as its first project.")
    init.add_argument("--owner", help="whose board it is (default: git user.name)")

    ls = command("ls", run_ls, "List the cards, column by column.")
    ls.add_argument("--state", choices=STATES)
    ls.add_argument("--project")

    show = command("show", run_show, "Show one card in full, with its comments.")
    show.add_argument("id")

    add = command("add", run_add, "Add a card. It goes last in its column unless --first.")
    add.add_argument("--project", required=True, action="append", help="repeat for work that spans projects")
    add.add_argument("--title", required=True)
    add.add_argument("--next", required=True, help="the next move, in one or two short sentences")
    add.add_argument("--state", choices=STATES, default="planning")
    add.add_argument("--first", action="store_true", help="put it at the top of its column")
    add_card_fields(add)

    set_ = command("set", run_set, "Change a card. It is marked checked today.")
    set_.add_argument("id")
    set_.add_argument("--state", choices=STATES)
    set_.add_argument("--title")
    set_.add_argument("--next")
    set_.add_argument("--project", action="append")
    set_.add_argument("--clear", action="append", default=[], choices=("pr", "ticket", "where", "waiting", "choices"))
    add_card_fields(set_)

    touch = command("touch", run_touch, "Mark a card checked today: it still holds.")
    touch.add_argument("id")

    rm = command("rm", run_rm, "Remove a card: the work was dropped, or it has been live for a week.")
    rm.add_argument("id")

    reply = command("reply", run_reply, "Record your answer to a comment, so it shows on the card.")
    reply.add_argument("comment")
    reply.add_argument("text")

    command(
        "trust",
        run_trust,
        "Let the Claude Code hooks use this board. Only for a board you control: "
        "its comments and RULES.md reach your sessions.",
    )

    command("lint", run_lint, "Check every card. Exits 1 if a card cannot be shown.")

    sync = command("sync", run_sync, "Move cards whose PRs opened or merged. Needs the gh CLI.")
    sync.add_argument("--dry-run", action="store_true")

    log = command("log", run_log, "What moved on the board, from the history.")
    log.add_argument("--days", type=int, default=7)

    serve = command("serve", run_serve, "Serve the board page on localhost.")
    serve.add_argument("--port", type=int, default=PORT)

    open_ = command("open", run_open, "Open the board page in your browser, with the link that lets it in.")
    open_.add_argument("--port", type=int, default=PORT)

    autostart = command("autostart", run_autostart, "macOS: keep the board page served from login on.")
    autostart.add_argument("--port", type=int, default=PORT)

    hook = command("hook", run_hook, "Run a Claude Code hook. Reads the hook's JSON on stdin.")
    hook.add_argument("event", choices=("session-start", "prompt", "stop"))
    return parser


def add_card_fields(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pr", type=int, action="append", help="a PR number; repeat for several")
    parser.add_argument("--ticket", help="the work's ticket ID in the team's tracker, like SUN-142")
    parser.add_argument("--where", help="the worktree folder the work happens in")
    parser.add_argument("--here", action="store_true", help="the worktree you are in")
    parser.add_argument("--waiting", help="who the work waits on, when it is not the owner")
    parser.add_argument("--choice", action="append", help="on a needs-you card, a reply the owner can pick")


def board_for(args: argparse.Namespace) -> Board:
    return boards.load(args.board) if args.board else boards.find(Path.cwd())


def card_fields(args: argparse.Namespace) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if args.project:
        fields["project"] = args.project[0] if len(args.project) == 1 else args.project
    for field in ("state", "title", "next", "ticket", "waiting"):
        if getattr(args, field, None) is not None:
            fields[field] = getattr(args, field)
    if args.pr:
        fields["pr"] = args.pr[0] if len(args.pr) == 1 else args.pr
    if args.here:
        worktree = git.current(Path.cwd())
        if worktree is None:
            raise BoardError("--here needs to run inside a git worktree.")
        fields["where"] = worktree.name
    elif args.where is not None:
        fields["where"] = args.where
    if args.choice:
        fields["choices"] = args.choice
    return fields


def by() -> str:
    worktree = git.current(Path.cwd())
    return worktree.name if worktree else "cli"


def run_init(args: argparse.Namespace) -> int:
    directory = args.board or Path.cwd() / boards.DIR_NAME
    if (directory / boards.CONFIG_FILE).exists():
        raise BoardError(f"There is already a board in {directory}.")
    cwd = Path.cwd()
    worktree = git.current(cwd)
    repos = [worktree.repo] if worktree and worktree.repo else git.repos_at_or_below(directory.parent)
    if not repos:
        raise BoardError(
            f"No GitHub repo at or below {directory.parent}. Run `yourmove init` inside a repo with an origin, "
            "or in the folder that holds your repos."
        )
    projects = project_entries(repos)
    owner = args.owner or git.git(cwd, "config", "user.name") or "me"
    directory.mkdir(parents=True)
    (directory / boards.CONFIG_FILE).write_text(json.dumps({"owner": owner, "projects": projects}, indent=2) + "\n")
    (directory / boards.CARDS_FILE).touch()
    (directory / ".gitignore").write_text(GITIGNORE)
    user.trust(directory)
    print(f"Made a board in {directory}, owned by {owner}, with {len(projects)} project(s): {', '.join(projects)}.")
    print(f"Rename or add projects in {directory / boards.CONFIG_FILE}.")
    print("Then `yourmove serve`, and `yourmove open` to see it.")
    return 0


# Committing a board is fine. These are this machine's alone.
GITIGNORE = """server.log
.lock/
.*.tmp
"""

PROJECT_COLORS = ("#3b82d6", "#1f9d63", "#e0674c", "#d99a00", "#7c4dcc", "#0e9aa7", "#c2410c", "#64748b")


def project_entries(repos: list[str]) -> dict[str, dict[str, str]]:
    """One project per repo, keyed by its name, or owner-name when two repos share a name."""
    names = [repo.split("/")[-1].lower() for repo in repos]
    projects = {}
    for index, repo in enumerate(repos):
        key = names[index] if names.count(names[index]) == 1 else repo.replace("/", "-").lower()
        projects[key] = {
            "name": key.replace("-", " ").title(),
            "color": PROJECT_COLORS[index % len(PROJECT_COLORS)],
            "repo": repo,
        }
    return projects


def run_ls(args: argparse.Namespace) -> int:
    board = board_for(args)
    cards = boards.read(board)
    shown = [
        card
        for card in cards.cards
        if (not args.state or card["state"] == args.state)
        and (not args.project or args.project in boards.projects_of(card))
    ]
    for state in STATES:
        column = [card for card in shown if card["state"] == state]
        if not column:
            continue
        print(f"{state} ({len(column)})")
        for card in column:
            print(f"  {card['id']}  {card['title']}  [{facts(card)}]")
            print(f"    {card['next']}")
    for problem in cards.errors:
        print(f"! {problem.message}", file=sys.stderr)
    return 0


def facts(card: Card) -> str:
    parts = [",".join(boards.projects_of(card))]
    parts += [f"#{pr}" for pr in boards.prs_of(card)]
    if card.get("ticket"):
        parts.append(card["ticket"])
    if card.get("where"):
        parts.append(card["where"])
    if card.get("waiting"):
        parts.append(f"waiting on {card['waiting']}")
    parts.append(f"checked {card['updated']}")
    return ", ".join(parts)


def run_show(args: argparse.Namespace) -> int:
    board = board_for(args)
    card = boards.read(board).find(args.id)
    print(f"{card['title']}  ({card['state']})")
    print(f"  {facts(card)}")
    print(f"  Next: {card['next']}")
    for choice in card.get("choices", []):
        print(f"  Choice: {choice}")
    for comment in notes.load(board):
        if comment["card"] == card["id"]:
            print(f"  Comment {comment['id']} ({comment['at']}): {comment['text']}")
            if "reply" in comment:
                print(f"    Reply: {comment['reply']['text']}")
    return 0


def run_add(args: argparse.Namespace) -> int:
    card = boards.add(board_for(args), card_fields(args), first=args.first)
    print(f"Added {card['id']} to {card['state']}.")
    return 0


def run_set(args: argparse.Namespace) -> int:
    fields: dict[str, Any] = {**card_fields(args), **{field: None for field in args.clear}}
    card = boards.update(board_for(args), args.id, fields, by=by())
    print(f"{card['id']} is {card['state']}, checked today.")
    return 0


def run_touch(args: argparse.Namespace) -> int:
    boards.update(board_for(args), args.id, {}, by=by())
    print(f"{args.id} checked today.")
    return 0


def run_rm(args: argparse.Namespace) -> int:
    card = boards.remove(board_for(args), args.id, by=by())
    print(f'Removed "{card.get("title", args.id)}".')
    return 0


def run_reply(args: argparse.Namespace) -> int:
    comment = notes.reply(board_for(args), args.comment, args.text)
    print(f'Reply saved on "{comment["title"]}".')
    return 0


def run_trust(args: argparse.Namespace) -> int:
    board = board_for(args)
    user.trust(board.dir)
    print(f"Trusted {board.dir.resolve()}. Claude Code sessions below it are now briefed from it.")
    return 0


def run_lint(args: argparse.Namespace) -> int:
    board = board_for(args)
    cards = boards.read(board)
    for problem in cards.errors:
        print(f"error: {problem.message}")
    warnings = [(card, warning) for card in cards.cards for warning in boards.warnings_in(card, date.today())]
    for card, warning in warnings:
        print(f"warning: {card['id']} {warning}")
    print(f"{len(cards.cards)} cards, {len(cards.errors)} errors, {len(warnings)} warnings.")
    return 1 if cards.errors else 0


def run_sync(args: argparse.Namespace) -> int:
    if not github.available():
        raise BoardError("sync needs the GitHub CLI: https://cli.github.com, then `gh auth login`.")
    board = board_for(args)
    moves, unclear = github.plan(board)
    made = moves if args.dry_run else github.apply(board, moves)
    verb = "Would move" if args.dry_run else "Moved"
    for move in made:
        print(f"{verb} {move.card['id']} from {move.card['state']} to {move.to}: {move.why}.")
    for note in unclear:
        print(note)
    if not made and not unclear:
        print("Every card with a PR is where its PR says.")
    return 0


def run_log(args: argparse.Namespace) -> int:
    for entry in boards.history(board_for(args), since=date.today() - timedelta(days=args.days)):
        who = f" ({entry['by']})" if entry.get("by") else ""
        if entry["event"] == "move":
            print(f"{entry['at']}  {entry['title']}: {entry['from']} -> {entry['to']}{who}")
        elif entry["event"] == "add":
            print(f"{entry['at']}  {entry['title']}: added in {entry['to']}{who}")
        else:
            print(f"{entry['at']}  {entry['title']}: removed{who}")
    return 0


def run_serve(args: argparse.Namespace) -> int:
    from . import server

    server.serve(board_for(args), args.port)
    return 0


def run_autostart(args: argparse.Namespace) -> int:
    from . import autostart

    print(autostart.install(board_for(args), args.port))
    return 0


def run_open(args: argparse.Namespace) -> int:
    from . import server

    token = user.server_token(args.port)
    # Another account could be listening on the port while the owner's server is down.
    if not server.is_owners_server(args.port, token):
        raise BoardError(
            f"Your board server is not running on port {args.port}. Start it with `yourmove serve` "
            "(or `yourmove autostart`), then open it again."
        )
    # Every account on a machine can read a command line, so the browser is never handed the token in one.
    # It opens a file only this user can read, which forwards to the link, the way Jupyter does.
    url = html.escape(f"http://127.0.0.1:{args.port}/?token={token}")
    forward = user.private_dir(user.config_dir()) / "open.html"
    descriptor = os.open(forward, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(
            f'<!doctype html><meta http-equiv="refresh" content="0;url={url}"><a href="{url}">Open the board</a>\n'
        )
    webbrowser.open(forward.as_uri())
    print(f"Opened the board on port {args.port}. With no browser here, the link is in {forward}.")
    return 0


def run_hook(args: argparse.Namespace) -> int:
    print(hooks.run(args.event, json.load(sys.stdin)), end="")
    return 0
