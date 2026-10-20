"""Which worktree a session is in. Cards name worktrees, so this is how a session finds its card."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Worktree:
    name: str  # the worktree folder's name, which is what a card's "where" holds
    is_main: bool
    repo: str  # "owner/name" from origin, or "" when there is no origin
    head: str  # "" before the first commit
    siblings: frozenset[str]  # every live worktree of the same repo, this one included


def current(cwd: Path) -> Worktree | None:
    top = git(cwd, "rev-parse", "--show-toplevel")
    listing = git(cwd, "worktree", "list", "--porcelain")
    if top is None or listing is None:
        return None
    paths, heads = [], {}
    for line in listing.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line.removeprefix("worktree ")))
        elif line.startswith("HEAD ") and paths:
            heads[paths[-1]] = line.removeprefix("HEAD ")
    here = Path(top).resolve()
    resolved = [path.resolve() for path in paths]
    return Worktree(
        name=here.name,
        is_main=bool(resolved) and resolved[0] == here,
        repo=repo_slug(git(cwd, "remote", "get-url", "origin") or ""),
        head=next((heads[path] for path in paths if path.resolve() == here), ""),
        siblings=frozenset(path.name for path in resolved if path.exists()),
    )


def repo_slug(url: str) -> str:
    """owner/name from any remote URL: https, ssh, or scp-style git@host:owner/name."""
    match = re.search(r"[/:]([^/:]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def repos_at_or_below(root: Path) -> list[str]:
    """The GitHub repos at root or up to two folders below it, for a board kept above several repos."""
    folders = [root]
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            folders += [child, *(c for c in sorted(child.iterdir()) if c.is_dir() and not c.name.startswith("."))]
    slugs = []
    for folder in folders:
        if (folder / ".git").exists():
            slug = repo_slug(git(folder, "remote", "get-url", "origin") or "")
            if slug and slug not in slugs:
                slugs.append(slug)
    return slugs
