"""Keeps `yourmove serve` running from login on, through a macOS LaunchAgent."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .board import Board, BoardError

LABEL = "dev.yourmove.serve"


def install(board: Board) -> str:
    if sys.platform != "darwin":
        raise BoardError("autostart is macOS only. Elsewhere, run `yourmove serve` from your init system.")
    agent = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    log = board.path("server.log")
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_bytes(
        plistlib.dumps(
            {
                "Label": LABEL,
                "ProgramArguments": [sys.executable, "-m", "yourmove", "--board", str(board.dir), "serve"],
                # launchd starts with a bare PATH, and sync needs to find gh.
                "EnvironmentVariables": {
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                },
                "RunAtLoad": True,
                "KeepAlive": True,
                "StandardOutPath": str(log),
                "StandardErrorPath": str(log),
            }
        )
    )
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(agent)], check=True)
    return f"The board is served from login on at http://127.0.0.1:47600/. Its log is {log}."
