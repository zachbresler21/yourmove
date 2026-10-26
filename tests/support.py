from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from yourmove import board as boards

TODAY = date.today().isoformat()
LONG_AGO = (date.today() - timedelta(days=9)).isoformat()

CONFIG = {
    "owner": "Ada",
    "projects": {
        "shop": {"name": "Shop", "color": "#1f9d63", "repo": "acme/shop"},
        "blog": {"name": "Blog", "color": "#3b82d6", "repo": "acme/blog"},
    },
}


def card(**fields: Any) -> dict[str, Any]:
    made = {"id": "shop-checkout", "project": "shop", "state": "in-progress", "title": "Checkout", "next": "Build it."}
    made["updated"] = TODAY
    made.update(fields)
    return made


class BoardCase(unittest.TestCase):
    """A temp folder holding a .yourmove board, plus git repos made on demand."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        # Trust, secrets, session state and the login server are per user. Keep each test's in its own folder.
        env = {
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        self.dir = self.root / boards.DIR_NAME
        self.dir.mkdir()
        (self.dir / boards.CONFIG_FILE).write_text(json.dumps(CONFIG))
        self.board = boards.load(self.dir)

    def write_cards(self, *cards: dict[str, Any]) -> None:
        self.write_lines([boards.card_line(c) for c in cards])

    def write_lines(self, lines: list[str]) -> None:
        (self.dir / boards.CARDS_FILE).write_text("".join(f"{line}\n" for line in lines))

    def lines(self) -> list[str]:
        return (self.dir / boards.CARDS_FILE).read_text().splitlines()

    def repo(self, name: str, slug: str, worktrees: Sequence[str] = ()) -> Path:
        """A repo at <root>/<name>/<name>.wt with an origin and one sibling folder per worktree."""
        main = self.root / name / f"{name}.wt"
        main.mkdir(parents=True)
        run(main, "init", "-q", "-b", "main")
        run(main, "remote", "add", "origin", f"git@github.com:{slug}.git")
        commit(main, "first")
        for worktree in worktrees:
            run(main, "worktree", "add", "-q", "-b", f"feature/{worktree}", str(main.parent / worktree))
        return main


def run(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def commit(cwd: Path, message: str) -> None:
    run(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", message)
