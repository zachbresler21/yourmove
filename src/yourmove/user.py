"""What has to live outside every board: which boards this user trusts, and the key that signs comments.

A board is a folder anyone can commit to a repo. Hooks hand a board's comments to Claude as the
owner's own words, so a cloned repo must not be able to ship a board of its own and speak for you.
Trust is granted per board on this machine, and a comment counts only if this machine's board
server signed it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

SIGNED_FIELDS = ("id", "card", "title", "where", "project", "text", "at")


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "yourmove"


def state_dir() -> Path:
    # Plugin data survives plugin updates. Either way it sits in the user's home, never a shared /tmp.
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data:
        return Path(data)
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "yourmove"


def private_dir(directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def trusted_file() -> Path:
    return config_dir() / "trusted-boards"


def is_trusted(board_dir: Path) -> bool:
    try:
        lines = trusted_file().read_text().splitlines()
    except FileNotFoundError:
        return False
    return str(board_dir.resolve()) in lines


def trust(board_dir: Path) -> None:
    if is_trusted(board_dir):
        return
    private_dir(config_dir())
    with trusted_file().open("a") as file:
        file.write(f"{board_dir.resolve()}\n")


def key() -> bytes:
    """This machine's comment key, made on first use and readable by this user only."""
    path = private_dir(config_dir()) / "comment-key"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    made = secrets.token_hex(32).encode()
    with os.fdopen(descriptor, "wb") as file:
        file.write(made)
    return made


def signature(comment: dict[str, Any]) -> str:
    signed = json.dumps([comment.get(field) for field in SIGNED_FIELDS], ensure_ascii=False)
    return hmac.new(key(), signed.encode(), hashlib.sha256).hexdigest()


def is_signed(comment: dict[str, Any]) -> bool:
    return hmac.compare_digest(str(comment.get("sig", "")), signature(comment))
