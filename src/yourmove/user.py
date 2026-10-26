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
import re
import secrets
from pathlib import Path
from typing import Any

from .board import BoardError

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
    """The key that signs comments. Replacing it unsigns every comment signed before."""
    return secret("comment-key")


def server_token() -> str:
    """What the board page sends with every request, so other accounts on this machine cannot use the server."""
    return secret("server-token").decode()


def secret(name: str) -> bytes:
    """A random secret in the config folder, made on first use and readable by this user only."""
    path = private_dir(config_dir()) / name
    if not path.exists():
        # Written whole beside the secret, then linked into place, so a reader finds none or all of one.
        temp = path.with_name(f".{name}.{os.getpid()}.{secrets.token_hex(4)}")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(secrets.token_hex(32).encode())
        try:
            os.link(temp, path)
        except FileExistsError:
            pass  # Another process linked its secret first. That one is the secret.
        finally:
            temp.unlink()
    made = path.read_bytes()
    # A short or empty secret would let anyone forge a comment or use the server, so never use one.
    if not re.fullmatch(rb"[0-9a-f]{64}", made):
        raise BoardError(f"{path} is not a yourmove secret. Delete it and yourmove makes a new one.")
    return made


def signature(comment: dict[str, Any], board_dir: Path) -> str:
    # The board is signed too, so a comment copied onto another board does not count there.
    signed = json.dumps(
        [str(board_dir.resolve()), *(comment.get(field) for field in SIGNED_FIELDS)], ensure_ascii=False
    )
    return hmac.new(key(), signed.encode(), hashlib.sha256).hexdigest()


def is_signed(comment: dict[str, Any], board_dir: Path) -> bool:
    return hmac.compare_digest(str(comment.get("sig", "")), signature(comment, board_dir))


def delivery_file() -> Path:
    return state_dir() / "delivered-comments"


def deliveries() -> dict[str, str]:
    """Which session each comment went to, by signature.

    Kept here and not on the board, where anyone who can edit the file could clear a delivery
    and have an old comment handed to a session again.
    """
    try:
        lines = delivery_file().read_text().splitlines()
    except FileNotFoundError:
        return {}
    return dict(line.split(" ", 1) for line in lines if " " in line)


def record_deliveries(signatures: list[str], session: str) -> None:
    private_dir(state_dir())
    with delivery_file().open("a") as file:
        file.writelines(f"{sig} {session}\n" for sig in signatures)
