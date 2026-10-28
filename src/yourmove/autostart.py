"""Keeps `yourmove serve` running from login on, through a macOS LaunchAgent."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from xml.parsers.expat import ExpatError

from .board import PORT, Board, BoardError, open_command

LABEL = "dev.yourmove.serve"
SOURCE = Path(__file__).resolve().parents[1]  # the src folder of this copy of yourmove
BOOTSTRAP_TRIES = 3


def agent_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(board: Board, port: int) -> str:
    if sys.platform != "darwin":
        raise BoardError("autostart is macOS only. Elsewhere, run `yourmove serve` from your init system.")
    environment = {
        "PYTHONPATH": str(SOURCE),
        # launchd starts with a bare PATH, and sync needs to find gh.
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    # The server's token and comment key live in the config folder, which this can move.
    if "XDG_CONFIG_HOME" in os.environ:
        environment["XDG_CONFIG_HOME"] = os.environ["XDG_CONFIG_HOME"]
    start(board.dir, port, environment)
    return (
        f"The board is served from login on at http://127.0.0.1:{port}/. Open it with {open_command(port)}. "
        f"Its log is {board.path('server.log')}."
    )


def read_agent(file: Path) -> tuple[Path, int, dict[str, str]] | None:
    """None means this is not a LaunchAgent this code wrote, so callers leave it alone."""
    try:
        agent = plistlib.loads(file.read_bytes())
        environment = dict(agent["EnvironmentVariables"])
        arguments = list(agent["ProgramArguments"])
        if arguments[1:3] != ["-m", "yourmove"] or "serve" not in arguments:
            return None
        if not isinstance(environment.get("PYTHONPATH"), str):
            return None
        board_dir = Path(arguments[arguments.index("--board") + 1])
        port = int(arguments[arguments.index("--port") + 1]) if "--port" in arguments else PORT
    except (OSError, plistlib.InvalidFileException, ExpatError, KeyError, IndexError, TypeError, ValueError):
        return None
    return board_dir, port, environment


def refresh() -> None:
    """Move the login server onto this copy of yourmove when the copy it runs is gone or older.

    A plugin update puts the new version in a folder beside the old one and deletes the old one
    later. Until then the server would run old code, and after that, no code at all.
    """
    file = agent_file()
    if sys.platform != "darwin" or not file.exists():
        return
    agent = read_agent(file)
    if agent is None:
        return
    board_dir, port, environment = agent
    running = Path(environment["PYTHONPATH"])
    if running.exists() and not is_older_version(running):
        return
    start(board_dir, port, {**environment, "PYTHONPATH": str(SOURCE)})


def is_older_version(other: Path) -> bool:
    """Whether other is the src folder of an older version of this plugin, installed beside this one.

    Never a clone, and never a newer version: a session still running the old plugin must not move the server back.
    """
    if other.parent.parent != SOURCE.parent.parent:
        return False
    theirs, ours = version(other.parent.name), version(SOURCE.parent.name)
    return theirs is not None and ours is not None and theirs < ours


def version(name: str) -> tuple[int, ...] | None:
    parts = name.split(".")
    return tuple(int(part) for part in parts) if all(part.isdigit() for part in parts) else None


def start(board_dir: Path, port: int, environment: dict[str, str]) -> None:
    file = agent_file()
    log = board_dir / "server.log"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(
        plistlib.dumps(
            {
                "Label": LABEL,
                "ProgramArguments": [
                    sys.executable,
                    "-m",
                    "yourmove",
                    "--board",
                    str(board_dir),
                    "serve",
                    "--port",
                    str(port),
                ],
                "EnvironmentVariables": environment,
                "RunAtLoad": True,
                "KeepAlive": True,
                "StandardOutPath": str(log),
                "StandardErrorPath": str(log),
            }
        )
    )
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    # A bootstrap made while the bootout is still finishing fails with an I/O error, and works a moment later.
    for _ in range(BOOTSTRAP_TRIES):
        result = subprocess.run(["launchctl", "bootstrap", domain, str(file)], capture_output=True, text=True)
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise BoardError(
        f"launchctl could not start the board server: {result.stderr.strip()}. Run `yourmove autostart` to try again."
    )
