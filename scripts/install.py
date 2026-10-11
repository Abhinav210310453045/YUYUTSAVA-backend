#!/usr/bin/env python
"""Install YUYUTSAVA from this checkout — the one way in, for the desktop app and the terminal.

    python scripts/install.py              # both: the desktop app and the terminal command
    python scripts/install.py --ui         # the desktop app only
    python scripts/install.py --cli        # the terminal command only
    python scripts/install.py --extra dev  # plus the test tools
    python scripts/install.py --lean       # no optional features
    python scripts/install.py --dry-run    # print the commands only

Run it again after pulling: a changed lock is picked up the same way. Both
installs come from ``uv.lock``, with the same features:

* ``--ui`` — **the checkout's ``.venv``**, which the desktop app runs its daemon
  in (``uv run``). Synced frozen (never re-resolved) and *inexact*: nothing
  already installed is ever taken away. A plain ``uv sync`` is exact, and
  uninstalls every optional feature its command line does not name — voice,
  vision, memory — so it is never the way to install.
* ``--cli`` — **the ``yuyutsava`` command on your PATH**: the lock exported as
  constraints and the checkout installed under them, editable, so the command
  runs this checkout's code on exactly the locked dependencies. ``pipx install
  -e .`` or a bare ``uv tool install -e .`` resolve the newest versions their
  ranges allow, which is how a daemon came to run mcp 2.2.0 against a lock of
  1.26.0 and every MCP server failed (R-BUG-28).

By default both get every feature the product has (``PRODUCT_EXTRAS``). The
daemon warns at start-up when an install has drifted from the lock.

If an earlier terminal install came from pipx, remove it first (``pipx
uninstall yuyutsava``): both put a ``yuyutsava`` executable in the same bin
directory. Needs uv.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Every feature a user of the product has: each model provider's SDK, voice, the
#: webcam source, semantic memory on Postgres, charts. ``dev`` (tests) and
#: ``streamlit`` are for working on YUYUTSAVA itself; add them with ``--extra``.
PRODUCT_EXTRAS = ("all-providers", "voice", "vision", "memory", "visuals")


def ui_commands(extras: list[str]) -> list[list[str]]:
    """The desktop app's ``.venv``: the lock, frozen and inexact."""
    return [["uv", "sync", "--frozen", "--inexact", *_flags(extras)]]


def cli_commands(extras: list[str], constraints: Path) -> list[list[str]]:
    """The terminal command: export the lock, install the checkout under it."""
    target = f".[{','.join(extras)}]" if extras else "."
    return [
        [
            "uv",
            "export",
            "--frozen",
            "--no-hashes",
            "--no-emit-project",
            "--no-dev",
            *_flags(extras),
            "--output-file",
            str(constraints),
        ],
        [
            "uv",
            "tool",
            "install",
            "--force",
            "--editable",
            target,
            "--constraints",
            str(constraints),
        ],
    ]


def _flags(extras: list[str]) -> list[str]:
    return [flag for extra in extras for flag in ("--extra", extra)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ui", action="store_true", help="install the desktop app's environment")
    parser.add_argument("--cli", action="store_true", help="install the terminal command")
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        help="another optional-dependency group to include (repeatable), e.g. dev",
    )
    parser.add_argument(
        "--lean", action="store_true", help="none of the optional features, only what --extra names"
    )
    parser.add_argument("--dry-run", action="store_true", help="print the commands only")
    args = parser.parse_args(argv)
    extras = list(dict.fromkeys([*(() if args.lean else PRODUCT_EXTRAS), *args.extra]))
    both = not (args.ui or args.cli)

    with tempfile.TemporaryDirectory() as tmp:
        steps = []
        if args.ui or both:
            steps += ui_commands(extras)
        if args.cli or both:
            steps += cli_commands(extras, Path(tmp) / "constraints.txt")
        for cmd in steps:
            print("$", shlex.join(cmd))
            if not args.dry_run:
                done = subprocess.run(cmd, cwd=REPO, check=False)
                if done.returncode != 0:
                    return done.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
