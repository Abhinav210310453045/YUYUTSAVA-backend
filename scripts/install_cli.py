#!/usr/bin/env python
"""Install the `yuyutsava` command on your PATH with the locked dependencies.

    python scripts/install_cli.py                 # base install
    python scripts/install_cli.py --extra vertex  # plus a provider's SDK
    python scripts/install_cli.py --dry-run       # print the commands only

`uv run yuyutsava …` from the checkout (and the desktop app, which starts the
daemon that way) already uses the locked environment. A command installed on
the PATH by `pipx install -e .` or a bare `uv tool install -e .` does not: it
resolves the newest versions its ranges allow, which is how a daemon came to run
mcp 2.2.0 against a lock of 1.26.0 and every MCP server failed (R-BUG-28). This
exports `uv.lock` as constraints and installs the tool under them, editable, so
the command runs this checkout's code on exactly the locked dependencies. The
daemon warns at start-up when an install has drifted.

If an earlier install came from pipx, remove it first (`pipx uninstall
yuyutsava`): both put a `yuyutsava` executable in the same bin directory.
Needs uv.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def commands(extras: list[str], constraints: Path) -> list[list[str]]:
    """The two uv invocations: export the lock, install under it."""
    extra_flags = [flag for extra in extras for flag in ("--extra", extra)]
    target = f".[{','.join(extras)}]" if extras else "."
    return [
        [
            "uv",
            "export",
            "--frozen",
            "--no-hashes",
            "--no-emit-project",
            "--no-dev",
            *extra_flags,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        help="an optional-dependency group to include (repeatable), e.g. vertex",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the commands only")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        for cmd in commands(args.extra, Path(tmp) / "constraints.txt"):
            print("$", shlex.join(cmd))
            if not args.dry_run:
                done = subprocess.run(cmd, cwd=REPO, check=False)
                if done.returncode != 0:
                    return done.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
