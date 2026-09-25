"""The ``yuyutsava daemon`` command line.

Its own module, with nothing heavier than :mod:`argparse` behind it, so the CLI
can parse it — answer ``--help``, ``--status`` and ``--stop`` — without
importing the daemon, which costs ~195 modules none of those need.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yuyutsava daemon",
        description="Run the always-on YUYUTSAVA daemon.",
    )
    p.add_argument(
        "--workspace",
        "-w",
        type=Path,
        default=Path.cwd(),
        help="Workspace root for the TaskRunner gateway (default: cwd).",
    )
    p.add_argument(
        "--no-ui",
        action="store_true",
        help="Headless mode: don't auto-open the Electron app; terminal-only fallback.",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="DEBUG-level logging to stderr.")
    p.add_argument(
        "--debug-plumbing",
        action="store_true",
        help=(
            "Show uvicorn / langgraph runtime / httpx logs (debugging only). "
            "Off by default. Also honoured via YUYUTSAVA_DEBUG_PLUMBING=1."
        ),
    )
    p.add_argument(
        "--voice",
        action="store_true",
        help="Enable voice channel (TTS + STT). Requires yuyutsava[voice] extras "
        "and PIPER_MODEL / STT_PROVIDER env vars.",
    )
    p.add_argument(
        "--status", action="store_true", help="Show the running daemon's PID, URLs, uptime; exit."
    )
    p.add_argument(
        "--stop",
        action="store_true",
        help="Send SIGTERM to the running daemon and wait for it to exit; exit.",
    )
    return p
