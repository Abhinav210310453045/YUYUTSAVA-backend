"""The ``yuyutsava`` command: something on screen first, then the rest.

Loading the CLI (LangChain, deepagents, the provider SDKs) takes over a second
before it can print anything. This module is all the command imports first: it
starts the start-up line (:mod:`yuyutsava.cli.startup_status`) for a run that
opens a chat, then loads :mod:`yuyutsava.cli.cli` and hands over. Other
commands (``models``, ``daemon``, ``--help``) start as before, with no line.
"""

from __future__ import annotations

import importlib
import sys

from yuyutsava.cli.startup_status import status


def main() -> int:
    """Console-script entry point (``pyproject.toml`` → ``yuyutsava``)."""
    if opens_a_chat(sys.argv[1:]):
        status.start("Starting YUYUTSAVA…")
    try:
        cli = importlib.import_module("yuyutsava.cli.cli")
        return cli.main()
    finally:
        status.done()


#: Flags that print something and leave, with no chat to start.
_NO_CHAT = frozenset(
    {"-h", "--help", "--list-sessions", "--delete-session", "--print-tools", "--list-scenarios"}
)


def opens_a_chat(argv: list[str]) -> bool:
    """Whether *argv* opens the chat: ``yuyutsava`` alone, or ``yuyutsava chat …``."""
    if not argv:
        return True
    return argv[0] == "chat" and not _NO_CHAT & set(argv)


__all__ = ["main", "opens_a_chat"]
